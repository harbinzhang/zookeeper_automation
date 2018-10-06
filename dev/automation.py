import os
import sys
import time
import functools
# import http.client
import httplib
import json
import random
import subprocess
import struct
import socket
import fcntl
import logging
import errno

import boto3
from botocore.exceptions import ClientError


DEV_MODE = True if os.environ.get("DEV_MODE", "") else False

ENV = os.environ.get("ENV", "")
APP_SPEC = os.environ.get("APP_SPEC", "")
BOTO_REGION = os.environ.get("BOTO_REGION", "")

CLUSTER = 'automation'
CLUSTER_SIZE = 3
RETRY_TIMES = 5
RETRY_INTERVAL = 20

if BOTO_REGION == "":
    BOTO_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2") # Need to change


def main():

    instance_id = get_metadata()["instanceId"]
    logging.info("Current Instance ID %s", instance_id)

    # get all private IPs
    ec2 = boto3.client('ec2', region_name=BOTO_REGION)

    ecs = boto3.client('ecs', region_name=BOTO_REGION)
    list_response = ecs.list_container_instances(
        cluster=CLUSTER
    )
    
    descriptions_response = ecs.describe_container_instances(
        cluster=CLUSTER,
        containerInstances=list_response['containerInstanceArns']
    )  
    # print(descriptions_response)

    # get Instances' info
    instance_ids = []
    for it in descriptions_response['containerInstances']:
        instance_ids.append(it['ec2InstanceId'])
    instances = ec2.describe_instances(InstanceIds=instance_ids)
    # print(instances)

    # get private IPs
    private_ips = get_info_from_instances(instances, "PrivateIpAddress")
    sorted_ips = sorted(private_ips, key=lambda item: socket.inet_aton(item[0]))
    assert len(sorted_ips) == CLUSTER_SIZE

    # produce myid based on sorted private ips
    local_ip = get_private_ipv4()
    myid = sorted_ips.index(local_ip) + 1
    print (myid, local_ip)
    prepare_myid(myid)
    

    # TODO: get public IP Address
    # TODO: add sync() here

    # get current public IP, or allocate a new elastic IP
    public_ip = get_public_ipv4(ec2, instance_id)
    print(public_ip)

    # sync() retry wait until all instances have public IP
    for i in range(RETRY_TIMES):
        public_ips_dict = get_id_to_public_ip(instances, private_ips)
        if len(public_ips_dict) == CLUSTER_SIZE:
            logging.info("All instances have public IP, ready to prepare config files")
            break
        else:
            logging.info("Still Waiting some instances get their public IP, retried: %d times", i)
            time.sleep(RETRY_INTERVAL)
    if len(public_ips_dict) != CLUSTER_SIZE:
        logging.error("#instances have public IP not equal to cluster size")
        return            
    print(public_ips_dict)

    prepare_zoocfg(myid, public_ips_dict)




def prepare_myid(myid):
    filename = "/ecs/data/zookeeper/data/myid"
    os.environ["ZOO_MY_ID"] = str(myid)
    with open(filename, "w") as f:
        f.write(str(myid))
        logging.info("myid %d prepared in %s", myid, filename)
 

def prepare_zoocfg(myid, public_ips_dict):
    filename = "/ecs/data/zookeeper/conf/zoo.cfg"
    zoo_servers = []
    with open(filename, "a") as f:
        f.write("\n")
        for id in range(1, CLUSTER_SIZE+1):
            if id == myid:
                f.write("server."+str(myid)+"=0.0.0.0:2888:3888\n")
                zoo_servers.append("server."+str(myid)+"=0.0.0.0:2888:3888")
            else:
                f.write("server."+str(id)+"="+str(public_ips_dict[id])+":2888:3888\n")
                zoo_servers.append("server."+str(id)+"="+str(public_ips_dict[id])+":2888:3888")
        logging.info("zoo.cfg prepared in %s", filename)
        os.environ["ZOO_SERVERS"] = ' '.join(zoo_servers)


def allocate_and_associate_eip(ec2, instance_id):
    try:
        allocation = ec2.allocate_address(Domain='vpc')
        print(allocation)
        response = ec2.associate_address(AllocationId=allocation['AllocationId'],
                                        InstanceId=instance_id)
        print(response)
        # logging.info(response)
        return allocation['PublicIp']
    except ClientError as e:
        print(e)    
        return None


def release_eip(ec2, allocation_id):
    try:
        response = ec2.release_address(AllocationId=allocation_id)
        print('Address released', allocation_id)
    except ClientError as e:
        print(e)  


def get_info_from_instances(instances, key):
    res = []
    for reservation in instances['Reservations']:
        for instance in reservation['Instances']:
            res.append(instance[key])
    return res


def get_id_to_public_ip(instances, priavte_ips):
    res = {}
    for reservation in instances['Reservations']:
        for instance in reservation['Instances']:
            try:
                res[priavte_ips.index(instance["PrivateIpAddress"]) + 1] = instance["PublicIpAddress"]
            except:
                logging.info("A instance has not public IP, need to wait")
                return res
    return res


# @functools.lru_cache(1)
def get_metadata():
    conn = httplib.HTTPConnection("169.254.169.254", timeout=10)
    conn.request("GET", "/latest/dynamic/instance-identity/document")
    r1 = conn.getresponse()
    return json.loads(r1.read().decode("utf-8"))


# @functools.lru_cache(1)
def get_public_ipv4(ec2, instance_id):
    conn = httplib.HTTPConnection("169.254.169.254", timeout=10)
    conn.request("GET", "/latest/meta-data/public-ipv4")
    r1 = conn.getresponse()
    res = r1.read().decode("utf-8")

    if len(res) < 20:
        logging.info("Already has a public IP: %s", res)
    else:
        res = allocate_and_associate_eip(ec2, instance_id)
        logging.info("Allocate a new elastic IP: %s", res)
    return res
    # public_ipv4 = ec2.describe_instances(InstanceIds=[instance_id])
    # print(public_ipv4['Reservations'][0]['Instances'][0]['PublicIpAddress'])


def get_private_ipv4():
    conn = httplib.HTTPConnection("169.254.169.254", timeout=10)
    conn.request("GET", "/latest/meta-data/local-ipv4")
    r1 = conn.getresponse()
    return r1.read().decode("utf-8")


if __name__ == "__main__":
    logging.basicConfig(
        format="[%(asctime)s %(levelname)s] %(message)s",
        level=logging.INFO,
        stream=sys.stdout
    )
    main()
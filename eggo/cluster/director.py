# Licensed to Big Data Genomics (BDG) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The BDG licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
import os
import sys
import time
from getpass import getuser
from tempfile import mkdtemp
from datetime import datetime
from cStringIO import StringIO

import boto.ec2
import boto.cloudformation
from boto.ec2.networkinterface import (
    NetworkInterfaceCollection, NetworkInterfaceSpecification)
from boto.exception import BotoServerError
from fabric.api import sudo, local, run, execute, put, open_shell, env

from eggo.cluster.config import (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
                                 EC2_KEY_PAIR, EC2_PRIVATE_KEY_FILE)


env.user = 'ec2-user'
env.key_filename = EC2_PRIVATE_KEY_FILE


def _sleep(start_time):
    elapsed = (datetime.now() - start_time).seconds
    if elapsed < 30:
        time.sleep(5)
    elif elapsed < 60:
        time.sleep(10)
    elif elapsed < 200:
        time.sleep(20)
    else:
        time.sleep(elapsed / 10.)


# CLOUDFORMATION UTIL

def wait_for_stack_status(cf_conn, stack_name, stack_status):
    sys.stdout.write(
        "Waiting for stack to enter '{s}' state.".format(s=stack_status))
    sys.stdout.flush()
    start_time = datetime.now()
    num_attempts = 0
    while True:
        _sleep(start_time)
        stack = cf_conn.describe_stacks(stack_name)[0]
        if stack.stack_status == stack_status:
            break
        num_attempts += 1
        sys.stdout.write(".")
        sys.stdout.flush()
    sys.stdout.write("\n")
    end_time = datetime.now()
    print "Stack is now in '{s}' state. Waited {t} seconds.".format(
        s=stack_status, t=(end_time - start_time).seconds)


def create_cf_connection(region):
    return boto.cloudformation.connect_to_region(region)


def create_cf_stack(cf_conn, stack_name, cf_template_path, availability_zone):
    try:
        if len(cf_conn.describe_stacks(stack_name)) > 0:
            print "Stack '{n}' already exists. Reusing.".format(n=stack_name)
            return
    except BotoServerError:
        # stack does not exist
        pass

    print "Creating stack with name '{n}'.".format(n=stack_name)
    with open(cf_template_path, 'r') as template_file:
        template_body=template_file.read()
    cf_conn.create_stack(stack_name, template_body=template_body,
                         parameters=[('KeyPairName', EC2_KEY_PAIR),
                                     ('AZ', availability_zone)],
                         tags={'owner': getuser(),
                               'ec2_key_pair': EC2_KEY_PAIR})
    wait_for_stack_status(cf_conn, stack_name, 'CREATE_COMPLETE')


def get_stack_resource_id(cf_conn, stack_name, logical_resource_id):
    for resource in cf_conn.describe_stack_resources(stack_name):
        if resource.logical_resource_id == logical_resource_id:
            return resource.physical_resource_id
    return None


def get_subnet_id(cf_conn, stack_name):
    return get_stack_resource_id(cf_conn, stack_name, 'DMZSubnet')


def get_security_group_id(cf_conn, stack_name):
    return get_stack_resource_id(cf_conn, stack_name, 'ClusterSG')


def delete_stack(cf_conn, stack_name):
    print "Deleting stack with name '{n}'.".format(n=stack_name)
    cf_conn.delete_stack(stack_name)
    wait_for_stack_status(cf_conn, stack_name, 'DELETE_COMPLETE')


# EC2 UTIL

def create_ec2_connection(region):
    return boto.ec2.connect_to_region(region)


def get_instances(ec2_conn, tag_key, tag_value=''):
    rezzies = ec2_conn.get_all_reservations(
        filters={'tag:' + tag_key: tag_value})
    instances = itertools.chain.from_iterable([r.instances for r in rezzies])
    return [i for i in instances
            if i.state not in ["shutting-down", "terminated"]]


def get_launcher_instance(ec2_conn):
    return get_instances(ec2_conn, 'eggo_node_type', 'launcher')[0]


def get_manager_instance(ec2_conn):
    return get_instances(ec2_conn, 'eggo_node_type', 'manager')[0]


def get_worker_instances(ec2_conn):
    return get_instances(ec2_conn, 'eggo_node_type', 'worker')


def get_master_instance(ec2_conn):
    return get_instances(ec2_conn, 'eggo_node_type', 'master')[0]


def wait_for_instance_state(ec2_conn, instance, state='running'):
    sys.stdout.write(
        "Waiting for instance to enter '{s}' state.".format(s=state))
    sys.stdout.flush()
    start_time = datetime.now()
    num_attempts = 0
    while True:
        _sleep(start_time)
        instance.update()
        statuses = ec2_conn.get_all_instance_status(instance.id)
        if len(statuses) > 0:
            status = statuses[0]
            if (instance.state == state and
                    status.system_status.status == 'ok' and
                    status.instance_status.status == 'ok'):
                break
        num_attempts += 1
        sys.stdout.write(".")
        sys.stdout.flush()
    sys.stdout.write("\n")
    end_time = datetime.now()
    print "Instance is now in '{s}' state. Waited {t} seconds.".format(
        s=state, t=(end_time - start_time).seconds)


def install_private_key():
    put(EC2_PRIVATE_KEY_FILE, 'id.pem')
    run('chmod 600 id.pem')


def install_director_client():
    sudo('wget http://archive.cloudera.com/director/redhat/6/x86_64/director/'
         'cloudera-director.repo -O /etc/yum.repos.d/cloudera-director.repo')
    sudo('yum -y install cloudera-director-client')


def create_launcher_instance(ec2_conn, cf_conn, cf_stack_name, launcher_ami,
                             launcher_instance_type):
    launcher_instances = get_instances(ec2_conn, 'eggo_node_type', 'launcher')
    if len(launcher_instances) > 0:
        print "Launcher instance ({instance}) already exists. Reusing.".format(
            instance=launcher_instances[0].ip_address)
        return launcher_instances[0]
    
    print "Creating launcher instance."
    # see http://stackoverflow.com/questions/19029588/how-to-auto-assign-public-ip-to-ec2-instance-with-boto
    interface = NetworkInterfaceSpecification(
        subnet_id=get_subnet_id(cf_conn, cf_stack_name),
        groups=[get_security_group_id(cf_conn, cf_stack_name)],
        associate_public_ip_address=True)
    interfaces = NetworkInterfaceCollection(interface)
    reservation = ec2_conn.run_instances(
        launcher_ami,
        key_name=EC2_KEY_PAIR,
        instance_type=launcher_instance_type,
        network_interfaces=interfaces)
    launcher_instance = reservation.instances[0]
    
    launcher_instance.add_tag('user', getuser())
    launcher_instance.add_tag('ec2_key_pair', EC2_KEY_PAIR)
    launcher_instance.add_tag('eggo_cf_stack_name', cf_stack_name)
    launcher_instance.add_tag('eggo_node_type', 'launcher')
    wait_for_instance_state(ec2_conn, launcher_instance)
    execute(install_director_client, hosts=[launcher_instance.ip_address])
    execute(install_private_key, hosts=[launcher_instance.ip_address])
    return launcher_instance


def run_director_bootstrap(director_conf_path, region, cluster_ami,
                           num_workers, stack_name):
    # replace variables in conf template and copy to launcher
    cf_conn = create_cf_connection(region)
    params = {'accessKeyId': AWS_ACCESS_KEY_ID,
              'secretAccessKey': AWS_SECRET_ACCESS_KEY,
              'region': region,
              'keyName': EC2_KEY_PAIR,
              'subnetId': get_subnet_id(cf_conn, stack_name),
              'securityGroupsIds': get_security_group_id(cf_conn, stack_name),
              'image': cluster_ami,
              'num_workers': num_workers}
    with open(director_conf_path, 'r') as template_file:
         interpolated_body = template_file.read() % params
         director_conf = StringIO(interpolated_body)
    put(director_conf, 'director.conf')
    # bootstrap the Hadoop cluster
    run('cloudera-director bootstrap director.conf')


def provision(region, availability_zone, cf_stack_name, cf_template_path,
              launcher_ami, launcher_instance_type, director_conf_path,
              cluster_ami, num_workers):
    start_time = datetime.now()

    # create cloudformation stack (VPC etc)
    cf_conn = create_cf_connection(region)
    create_cf_stack(cf_conn, cf_stack_name, cf_template_path, availability_zone)

    # create launcher instance
    ec2_conn = create_ec2_connection(region)
    launcher_instance = create_launcher_instance(
        ec2_conn, cf_conn, cf_stack_name, launcher_ami, launcher_instance_type)

    # run bootstrap on launcher
    execute(
        run_director_bootstrap,
        director_conf_path=director_conf_path, region=region,
        cluster_ami=cluster_ami, num_workers=num_workers,
        stack_name=cf_stack_name, hosts=[launcher_instance.ip_address])

    end_time = datetime.now()
    print "Cluster has started. Took {t} minutes.".format(
        t=(end_time - start_time).seconds / 60)


def list(region):
    conn = create_ec2_connection(region)
    print 'Launcher', get_launcher_instance(conn).ip_address
    print 'Manager', get_manager_instance(conn).ip_address
    print 'Master', get_master_instance(conn).ip_address
    for instance in get_worker_instances(conn):
        print 'Worker', instance.ip_address


def get_worker_hosts(region):
    conn = create_ec2_connection(region)
    return [i.ip_address for i in get_worker_instances(conn)]


def login(region):
    conn = create_ec2_connection(region)
    hosts = [get_master_instance(conn).ip_address]
    execute(open_shell, hosts=hosts)


def web_proxy(instance, port):
    local('ssh -i {private_key} -o UserKnownHostsFile=/dev/null '
          '-o StrictHostKeyChecking=no -L {port}:{private_ip}:{port} '
          'ec2-user@{public_ip}'.format(
              private_key=EC2_PRIVATE_KEY_FILE, port=port,
              private_ip=instance.private_ip_address,
              public_ip=instance.ip_address))


def cm_web_proxy(region):
    ec2_conn = create_ec2_connection(region)
    instance = get_manager_instance(ec2_conn)
    web_proxy(instance, 7180)


# def hue_web_proxy(region):
#     web_proxy(region, 'master', 8888)


# def yarn_web_proxy(region):
#     web_proxy(region, 'master', 8088)


def run_director_terminate():
    run('cloudera-director terminate director.conf')


def terminate_launcher_instance(conn):
    launcher_instance = get_launcher_instance(conn)
    launcher_instance.terminate()
    wait_for_instance_state(conn, launcher_instance, 'terminated')


def teardown(region, cf_stack_name):
    # terminate Hadoop cluster (prompts for confirmation)
    conn = create_ec2_connection(region)
    execute(run_director_terminate, hosts=[get_launcher_instance(conn).ip_address])

    # terminate launcher instance
    terminate_launcher_instance(conn)

    # delete stack
    cf_conn = create_cf_connection(region)
    delete_stack(cf_conn, cf_stack_name)
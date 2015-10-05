#!/bin/env python

"""
The MIT License

Copyright (c) 2010 The Chicago Tribune & Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from multiprocessing import Pool
import os
import re
import socket
import time
import sys
IS_PY2 = sys.version_info.major == 2
if IS_PY2:
    from urllib2 import urlopen, Request
    from StringIO import StringIO
else:
    from urllib.request import urlopen, Request
    from io import StringIO
import base64
import csv
import random
import ssl
from contextlib import contextmanager
import traceback

import boto.ec2
import boto.exception
import paramiko

STATE_FILENAME = os.path.expanduser('~/.ants')

# Utilities

@contextmanager
def _redirect_stdout(outfile=None):
    save_stdout = sys.stdout
    sys.stdout = outfile or StringIO()
    yield
    sys.stdout = save_stdout

def _read_server_list():
    instance_ids = []

    if not os.path.isfile(STATE_FILENAME):
        return (None, None, None, None)

    with open(STATE_FILENAME, 'r') as f:
        username = f.readline().strip()
        key_name = f.readline().strip()
        zone = f.readline().strip()
        text = f.read()
        instance_ids = [i for i in text.split('\n') if i != '']

        print('Read %i bees from the roster.' % len(instance_ids))

    return (username, key_name, zone, instance_ids)

def _write_server_list(username, key_name, zone, instances):
    with open(STATE_FILENAME, 'w') as f:
        f.write('%s\n' % username)
        f.write('%s\n' % key_name)
        f.write('%s\n' % zone)
        f.write('\n'.join([instance.id for instance in instances]))

def _delete_server_list():
    os.remove(STATE_FILENAME)

def _get_pem_path(key):
    return os.path.expanduser('~/.ssh/%s.pem' % key)

def _get_region(zone):
    return zone if 'gov' in zone else zone[:-1] # chop off the "d" in the "us-east-1d" to get the "Region"

def _get_security_group_ids(connection, security_group_names, subnet):
    ids = []
    # Since we cannot get security groups in a vpc by name, we get all security groups and parse them by name later
    security_groups = connection.get_all_security_groups()

    # Parse the name of each security group and add the id of any match to the group list
    for group in security_groups:
        for name in security_group_names:
            if group.name == name:
                if subnet == None:
                    if group.vpc_id == None:
                        ids.append(group.id)
                    elif group.vpc_id != None:
                        ids.append(group.id)

        return ids

# Methods

def up(count, group, zone, image_id, instance_type, username, key_name, subnet, bid = None):
    """
    Startup the load testing server.
    """

    existing_username, existing_key_name, existing_zone, instance_ids = _read_server_list()

    count = int(count)
    if existing_username == username and existing_key_name == key_name and existing_zone == zone:
        # User, key and zone match existing values and instance ids are found on state file
        if count <= len(instance_ids):
            # Count is less than the amount of existing instances. No need to create new ones.
            print('Ants are already assembled and awaiting orders.')
            return
        else:
            # Count is greater than the amount of existing instances. Need to create the only the extra instances.
            count -= len(instance_ids)
    elif instance_ids:
        # Instances found on state file but user, key and/or zone not matching existing value.
        # State file only stores one user/key/zone config combination so instances are unusable.
        print('Taking down {} unusable ants.'.format(len(instance_ids)))
        # Redirect prints in down() to devnull to avoid duplicate messages
        with _redirect_stdout():
            down()
        # down() deletes existing state file so _read_server_list() returns a blank state
        existing_username, existing_key_name, existing_zone, instance_ids = _read_server_list()

    pem_path = _get_pem_path(key_name)

    if not os.path.isfile(pem_path):
        print('Warning. No key file found for %s. You will need to add this key to your SSH agent to connect.' % pem_path)

    print('Connecting to the hive.')

    try:
        ec2_connection = boto.ec2.connect_to_region(_get_region(zone))
    except boto.exception.NoAuthHandlerFound as e:
        print("Authenciation config error, perhaps you do not have a ~/.boto file with correct permissions?")
        print(e.message)
        return e
    except Exception as e:
        print("Unknown error occured:")
        print(e.message)
        return e

    if ec2_connection == None:
        raise Exception("Invalid zone specified? Unable to connect to region using zone name")

    if bid:
        print('Attempting to call up %i spot ants, this can take a while...' % count)

        spot_requests = ec2_connection.request_spot_instances(
            image_id=image_id,
            price=bid,
            count=count,
            key_name=key_name,
            security_groups=[group] if subnet is None else _get_security_group_ids(ec2_connection, [group], subnet),
            instance_type=instance_type,
            placement=None if 'gov' in zone else zone,
            subnet_id=subnet)

        # it can take a few seconds before the spot requests are fully processed
        time.sleep(5)

        instances = _wait_for_spot_request_fulfillment(ec2_connection, spot_requests)
    else:
        print('Attempting to call up %i ants.' % count)

        try:
            reservation = ec2_connection.run_instances(
                image_id=image_id,
                min_count=count,
                max_count=count,
                key_name=key_name,
                security_groups=[group] if subnet is None else _get_security_group_ids(ec2_connection, [group], subnet),
                instance_type=instance_type,
                placement=None if 'gov' in zone else zone,
                subnet_id=subnet)
        except boto.exception.EC2ResponseError as e:
            print("Unable to call ants:", e.message)
            return e

        instances = reservation.instances

    if instance_ids:
        existing_reservations = ec2_connection.get_all_instances(instance_ids=instance_ids)
        existing_instances = [r.instances[0] for r in existing_reservations]
        map(instances.append, existing_instances)

    print('Waiting for ants to spawn...')

    instance_ids = instance_ids or []

    for instance in [i for i in instances if i.state == 'pending']:
        instance.update()
        while instance.state != 'running':
            print('.')
            time.sleep(5)
            instance.update()

        instance_ids.append(instance.id)

        print('Ant %s is ready.' % instance.id)

    ec2_connection.create_tags(instance_ids, { "Name": "an ant!" })

    _write_server_list(username, key_name, zone, instances)

    print('The hive has assembled %i ants.' % len(instances))

def report():
    """
    Report the status of the load testing servers.
    """
    username, key_name, zone, instance_ids = _read_server_list()

    if not instance_ids:
        print('No ants have been mobilized.')
        return

    ec2_connection = boto.ec2.connect_to_region(_get_region(zone))

    reservations = ec2_connection.get_all_instances(instance_ids=instance_ids)

    instances = []

    for reservation in reservations:
        instances.extend(reservation.instances)

    for instance in instances:
        print('Ant %s: %s @ %s' % (instance.id, instance.state, instance.ip_address))

def down():
    """
    Shutdown the load testing server.
    """
    username, key_name, zone, instance_ids = _read_server_list()

    if not instance_ids:
        print('No ants have been mobilized.')
        return

    print('Connecting to the hive.')

    ec2_connection = boto.ec2.connect_to_region(_get_region(zone))

    print('Calling off the hive.')

    terminated_instance_ids = ec2_connection.terminate_instances(
        instance_ids=instance_ids)

    print('Stood down %i ants.' % len(terminated_instance_ids))

    _delete_server_list()

def _wait_for_spot_request_fulfillment(conn, requests, fulfilled_requests = []):
    """
    Wait until all spot requests are fulfilled.

    Once all spot requests are fulfilled, return a list of corresponding spot instances.
    """
    if len(requests) == 0:
        reservations = conn.get_all_instances(instance_ids = [r.instance_id for r in fulfilled_requests])
        return [r.instances[0] for r in reservations]
    else:
        time.sleep(10)
        print('.')

    requests = conn.get_all_spot_instance_requests(request_ids=[req.id for req in requests])
    for req in requests:
        if req.status.code == 'fulfilled':
            fulfilled_requests.append(req)
            print("spot ant `{}` joined the hive.".format(req.instance_id))

    return _wait_for_spot_request_fulfillment(conn, [r for r in requests if r not in fulfilled_requests], fulfilled_requests)

def _execute_order(params):
    print('Ant %i is joining the hive.' % params['i'])

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pem_path = params.get('key_name') and _get_pem_path(params['key_name']) or None
        if not os.path.isfile(pem_path):
            client.load_system_host_keys()
            client.connect(params['instance_name'], username=params['username'])
        else:
            client.connect(
                params['instance_name'],
                username=params['username'],
                key_filename=pem_path)

        print('Ant %i is executing order' % params['i'])

        """
        stdin, stdout, stderr = client.exec_command('mktemp')
        # paramiko's read() returns bytes which need to be converted back to a str
        params['csv_filename'] = IS_PY2 and stdout.read().strip() or stdout.read().decode('utf-8').strip()
        if params['csv_filename']:
            options += ' -e %(csv_filename)s' % params
        else:
            print('Bee %i lost sight of the target (connection timed out creating csv_filename).' % params['i'])
            return None

        if params['post_file']:
            pem_file_path=_get_pem_path(params['key_name'])
            os.system("scp -q -o 'StrictHostKeyChecking=no' -i %s %s %s@%s:/tmp/honeycomb"
                      "" % (pem_file_path, params['post_file'], params['username'], params['instance_name']))
            options += ' -T "%(mime_type)s; charset=UTF-8" -p /tmp/honeycomb' % params

        if params['keep_alive']:
            options += ' -k'

        if params['cookies'] is not '':
            options += ' -H \"Cookie: %s;sessionid=NotARealSessionID;\"' % params['cookies']
        else:
            options += ' -C \"sessionid=NotARealSessionID\"'

        if params['basic_auth'] is not '':
            options += ' -A %s' % params['basic_auth']

        params['options'] = options
        benchmark_command = 'ab -v 3 -r -n %(num_requests)s -c %(concurrent_requests)s %(options)s "%(url)s"' % params
"""
        stdin, stdout, stderr = client.exec_command(params['order'])

        #response = {}

        # paramiko's read() returns bytes which need to be converted back to a str
        #ab_results = IS_PY2 and stdout.read() or stdout.read().decode('utf-8')
        print(stdout.read().decode('utf-8'))

        client.close()

    except socket.error as e:
        return e
    except Exception as e:
        traceback.print_exc()
        print()
        raise e

def _execute_order_file(params):
    upload_path = "/tmp/"
    print('Ant %i is joining the hive.' % params['i'])

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pem_path = params.get('key_name') and _get_pem_path(params['key_name']) or None
        if not os.path.isfile(pem_path):
            client.load_system_host_keys()
            client.connect(params['instance_name'], username=params['username'])
        else:
            client.connect(
                params['instance_name'],
                username=params['username'],
                key_filename=pem_path)

        order_file = params['order_file']
        
        filename = os.path.basename(order_file)
        print('Ant %s uploading file %s to %s' % (params['i'], order_file, upload_path + filename))
        command = 'scp -i %s -o StrictHostKeyChecking=no %s %s@%s:%s' % (_get_pem_path(params['key_name']), order_file, params['username'], params['instance_name'], upload_path)
        os.system(command)
        
        print('Ant %s executing file %s' % (params['i'], upload_path + filename))
        stdin, stdout, stderr = client.exec_command('chmod +x %s'% upload_path + filename)
        stdin, stdout, stderr = client.exec_command(upload_path + filename)

        #response = {}

        # paramiko's read() returns bytes which need to be converted back to a str
        #ab_results = IS_PY2 and stdout.read() or stdout.read().decode('utf-8')
        print(stdout.read().decode('utf-8'))

        client.close()

    except socket.error as e:
        return e
    except Exception as e:
        traceback.print_exc()
        print()
        raise e

def order(orders, order_files):
    username, key_name, zone, instance_ids = _read_server_list()

    if not instance_ids:
        print('No ants are ready for orders.')
        return

    print('Connecting to the hive.')

    ec2_connection = boto.ec2.connect_to_region(_get_region(zone))

    print('Assembling ants.')

    reservations = ec2_connection.get_all_instances(instance_ids=instance_ids)

    instances = []

    for reservation in reservations:
        instances.extend(reservation.instances)

    instance_count = len(instances)

    params = []
    
    #Start with executing order
    if not orders == None:
        for order in orders:
            del params[:]
            for i, instance in enumerate(instances):
                params.append({
                    'i': i,
                    'instance_id': instance.id,
                    'instance_name': instance.private_dns_name if instance.public_dns_name == "" else instance.public_dns_name,
                    'username': username,
                    'key_name': key_name,
                    'order': order
            })

            print('Organizing the hive.')
            # Spin up processes for connecting to EC2 instances
            pool = Pool(len(params))
            results = pool.map(_execute_order, params)

    #Now run order files
    if not order_files == None:
        for order_file in order_files:
            print('Filename: %s' % order_file)
            del params[:]
            for i, instance in enumerate(instances):
                params.append({
                    'i': i,
                    'instance_id': instance.id,
                    'instance_name': instance.private_dns_name if instance.public_dns_name == "" else instance.public_dns_name,
                    'username': username,
                    'key_name': key_name,
                    'order_file': order_file
            })

            #print('Running order file %s' % order_file)

            print('Organizing the hive.')
            # Spin up processes for connecting to EC2 instances
            pool = Pool(len(params))
            results = pool.map(_execute_order_file, params)

    print('The hive is awaiting new orders.')

    sys.exit(0)

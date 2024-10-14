#from prometheus_client import start_http_server, Gauge, Counter
import requests
import os
import atexit
import re
import json
from time import sleep
from flask import Flask
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

requests.packages.urllib3.disable_warnings()

psm_ip = os.getenv('PSM_IP')
username = os.getenv('PSM_API_USER')
password = os.getenv('PSM_API_PASSWORD')
tenant = "default"
cookie_file = "cookies.txt"

def send_api_request(url, headers, body, cookie_file, method):
    # Generic module for sending a PSM API request
    session = requests.Session()

    try:
        response = session.request(
            method,
            url,
            headers=headers,
            data=body,
            verify=False
        )

        response.raise_for_status()

        if response.status_code == 200:
            return {'cookie':response.cookies , 'content':response.text }
        else:
            return f"Resource not found: {response.status_code} {response.text}"

    except requests.exceptions.RequestException as e:
        print(e)
        raise

    finally:
        session.close()


def login_psm(psm_ip, cookie_file, username, password, tenant):
    # Authenticate on PSM
    headers = {
        'Content-Type': 'application/json'
    }
    url = 'https://'+ psm_ip +'/v1/login'
    body = '{"username": "'+username+'","password": "'+password+'","tenant": "'+tenant+'"}'
    result = send_api_request(url, headers, body, "cookies.txt", "POST")
    sid = result["cookie"]["sid"]
    return sid


# Start the FLASK APP
app = Flask(__name__)
# Global Flask variables
app.config['psm_sid'] = login_psm(psm_ip, cookie_file, username, password, tenant)
app.config['psm_ip'] = psm_ip


def check_session_id():
    # Check the PSM Session ID and if it expires, create a new session
    url = 'https://'+ app.config['psm_ip'] +'/telemetry/v1/metrics'
    headers = {
        'Cookie': 'sid=' + app.config['psm_sid'],
        'Content-Type': 'application/json'
    }
    body = '{"queries":[{"kind":"Node","start-time":"now() - 2m","end-time":"now()"}]}'
    session = requests.Session()

    try:
        response = session.request( "POST", url, headers=headers, data=body, verify=False )

        if response.status_code != 200:
            app.config['psm_sid'] = login_psm(psm_ip, cookie_file, username, password, tenant)

    except requests.exceptions.RequestException as e:
        print(e)
        raise


# Scheduler which runs once every minute to verify that the PSM Session ID is valid
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_session_id, trigger="interval", seconds=60)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


def get_switches():
    # Get Switches, versions, status and store in a json
    # This is used to map Switch MAC Address to Switch Name
    switches = []
    sid = app.config['psm_sid']

    # Get DSS Status from PSM
    url = 'https://'+ app.config['psm_ip'] +'/configs/cluster/v1/distributedservicecards'

    headers = {
        'Cookie': 'sid=' + sid,
        'Content-Type': 'application/json'
    }

    switch_status_details = json.loads(send_api_request(url, headers, '', "cookies.txt", 'GET')['content'])

    for obj in switch_status_details["items"]:
        hostname = obj["status"]["dss-info"]["host-name"]
        health_status = obj["status"]["conditions"][0]["status"]
        if health_status == "true":
            switch_health = "UP"
        else:
            switch_health = "DOWN"
        dsc_version = obj["status"]["DSCVersion"]
        reporterID = obj["status"]["primary-mac"]
        switch_version = obj["status"]["dss-info"]["version"]

        switch_obj = {
            'name': hostname,
            'health_status': health_status,
            'dsc_version': dsc_version,
            'reporterId': reporterID,
            'switch_version': switch_version
        }

        switches.append(switch_obj)
    
    return switches

app.config['switches'] = get_switches()

def convert_time(time):
   # Convert date to int64 timestamp
   a = time.rsplit('.',1)
   c = a[0]+"."+a[1][:-4]
   timestamp = datetime.strptime(c, '%Y-%m-%dT%H:%M:%S.%f').timestamp() * 1000
   return timestamp

def get_reporter_id(switch_id, switches):
    # PSM metrics are associated with a primary switch MAC.  This function returns the Switch name based on the MAC
    for switch in switches:
        switch_mac = json.loads(json.dumps(switch))['reporterId']
        if switch_id == switch_mac:
            switch_name = json.loads(json.dumps(switch))['name']
            break
        else:
            switch_name = switch_id
    return switch_name

def get_columns(fields):
    # Get columns per metric kind and store in a json
    num_fields = 0
    for i in fields:
        num_fields = num_fields + 1

    columns = []

    for j in range(num_fields):
        field_obj = {
            'index': j,
            'field_name': fields[j]
        }
        columns.append(field_obj)

    return columns

def write_metrics(type, kind):
    # Write a metrics api output based on a json object.  type should be Switch or PSM
    url = 'https://'+ app.config['psm_ip'] +'/telemetry/v1/metrics'
    headers = {
        'Cookie': 'sid=' + app.config['psm_sid'],
        'Content-Type': 'application/json'
    }

    metrics = ""
    reporterID = ""
    psm_date = ""

    body = '{"queries":[{"kind":"'+kind+'","start-time":"now() - 2m","end-time":"now()"}]}'

    response = send_api_request(url, headers, body, "cookies.txt", 'GET')
    parsed_details = json.loads(response['content'])
    if 'series' in parsed_details["results"][0]:
        columns = parsed_details["results"][0]["series"][0]["columns"]
        values = parsed_details["results"][0]["series"][0]["values"]

        fields = get_columns(columns)
        num_columns = len(fields)
        
        for row in values:
            if type == "Switch":
                for i in range(1, int(num_columns-4)):
                    metric = row[i]
                    if metric == None:
                        metric = 0
                    psm_date = convert_time(row[0])
                    reporterID = get_reporter_id(row[num_columns-3], app.config['switches'])
                    unit = row[num_columns-1]
                    metrics += '%s_%s{node="%s"} %d %d\n' % (kind, fields[i]['field_name'], reporterID, metric, psm_date)
                    #metrics += '%s_%s{node="%s" network="%s"} %s\n' % (kind, fields[i]['field_name'], reporterID, row[num_columns-4], metric)
            else: #elif #type == "PSM":
                for i in range(1, int(num_columns-2)):
                    reporterID = row[num_columns-1]
                    metric = row[i]
                    if metric == None:
                        metric = 0
                    psm_date = convert_time(row[0])
                    metrics += '%s_%s{node="%s"} %d %d\n' % (kind, fields[i]['field_name'], reporterID, metric, psm_date)
                    #metrics += '%s_%s{node="%s"} %d %d\n' % (kind, fields[i]['field_name'], reporterID, metric, psm_date)
    return metrics

@app.route('/switch-metrics')
def switch_metrics():
    # API for getting the Switch Metrics
    metrics = ""
    metrics += write_metrics("Switch", "PowerMetrics")
    metrics += write_metrics("Switch", "AsicTemperatureMetrics")
    metrics += write_metrics("Switch", "LifMetrics")
    metrics += write_metrics("Switch", "EgressDrops")
    metrics += write_metrics("Switch", "IngressDrops")
    metrics += write_metrics("Switch", "FlowStatsSummary")
    metrics += write_metrics("Switch", "DataPathAssistStats")
    metrics += write_metrics("Switch", "VnicDrops")
    metrics += write_metrics("Switch", "MemoryMetrics")
    metrics += write_metrics("Switch", "AsicCpuMetrics")
    metrics += write_metrics("Switch", "MacMetrics")
    metrics += write_metrics("Switch", "IPsecEncryptMetrics")
    metrics += write_metrics("Switch", "IPsecDecryptMetrics")
    metrics += write_metrics("Switch", "RuleMetrics")

    # Get DSS Status from PSM
    url = 'https://'+ app.config['psm_ip'] +'/configs/cluster/v1/distributedservicecards'

    headers = {
        'Cookie': 'sid=' + app.config['psm_sid'],
        'Content-Type': 'application/json'
    }

    switch_status_details = json.loads(send_api_request(url, headers, '', "cookies.txt", 'GET')['content'])

    for obj in switch_status_details["items"]:
        hostname = obj["status"]["dss-info"]["host-name"]
        health_status = obj["status"]["conditions"][0]["status"]
        if health_status == "true":
            switch_health = 1
        else:
            switch_health = 0
        dsc_version = obj["status"]["DSCVersion"]
        reporterID = get_reporter_id(obj["status"]["primary-mac"], app.config['switches'])
        switch_version = obj["status"]["dss-info"]["version"]
        serial = obj["status"]["serial-num"]
        forwarding_profile = obj["status"]["dss-info"]["forwarding-profile"]

        metrics += 'DSC_health_status{node="%s" health_status="%s"} %s\n' % (reporterID, switch_health, switch_health)
        metrics += 'DSC_dsc_version{node="%s" dsc_version="%s"} 1\n' % (reporterID, dsc_version)
        metrics += 'DSC_switch_version{node="%s" switch_version="%s"} 1\n' % (reporterID, switch_version)
        metrics += 'DSC_node{node="%s" serial="%s" forwarding_profile="%s" health_status="%s" dsc_version="%s" switch_version="%s"} 1\n' % (reporterID, serial, forwarding_profile, switch_health, dsc_version, switch_version) 

    # Try to find out if ELBA is enabled
    url = 'https://'+ app.config['psm_ip'] +'/configs/network/v1/tenant/default/networks'

    body = ''

    network_objects = (json.loads(send_api_request(url, headers, body, cookie_file, 'GET')['content']))['items']

    elba_enabled = 0
    network_counter = 0
    # To figure out if Elba is enabled, we get all networks and check whether at least one does not have Service Bypass enabled
    for obj in network_objects:
        if obj['kind'] == 'Network':
            network_counter = network_counter + 1
            if 'service-bypass' in obj['spec']:
                if obj['spec']['service-bypass'] == False:
                    elba_enabled = 1
                    break
            else:
                elba_enabled = 1

    metrics += 'DSC_ELBA_enabled{elba_enabled="%s"} %s\n' % (elba_enabled, elba_enabled)
    metrics += 'DSC_Number_of_networks{num_networks="%d"} %d\n' % (network_counter, network_counter)

    # Count the VRFs
    url = 'https://'+ app.config['psm_ip'] +'/configs/network/v1/tenant/default/virtualrouters'
    vrf_counter = 0
    network_objects = (json.loads(send_api_request(url, headers, body, cookie_file, 'GET')['content']))['items']
    for obj in network_objects:
        if obj['kind'] == 'VirtualRouter':
            vrf_counter = vrf_counter + 1
    metrics += 'DSC_Number_of_vrfs{num_vrfs="%d"} %d' % (vrf_counter, vrf_counter)

    metrics += 'DSC_config_info{num_vrfs="%d" num_networks="%d" elba_enabled="%s"} 1\n' % (vrf_counter, network_counter, elba_enabled)

    return metrics

@app.route('/psm-metrics')
def psm_metrics():
    # API for getting PSM Metrics
    metrics = ""
    metrics += write_metrics("PSM", "Cluster")
    metrics += write_metrics("PSM", "Node")
    # Get some PSM cluster info
    url = 'https://'+ app.config['psm_ip'] +'/configs/cluster/v1/cluster'
    headers = {
        'Cookie': 'sid=' + app.config['psm_sid'],
        'Content-Type': 'application/json'
    }

    cluster_object = (json.loads(send_api_request(url, headers, '', 'cookies.txt', 'GET')['content']))
    for node in cluster_object['status']['quorum-status']['members']:
        if node['conditions'][0]['status'] == "true":
            status = "Up"
        else:
            status = "Down"
        metrics += 'Cluster_Node_IP{node_ip="%s" health_status="%s"} 1\n' % (node['name'], status)

    return metrics


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)

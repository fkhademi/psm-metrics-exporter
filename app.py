#from prometheus_client import start_http_server, Gauge, Counter
import requests
import re
import json
from time import sleep
from flask import Flask
from datetime import datetime

requests.packages.urllib3.disable_warnings()

psm_ip = '10.9.20.71'
username = 'admin'
password = 'Pensando0$'
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


app = Flask(__name__)

# Generate PSM Session ID to be used for gathering metrics
app.config['psm_sid'] = login_psm(psm_ip, cookie_file, username, password, tenant)
app.config['psm_ip'] = psm_ip


def get_switches():
    # Get Switches, versions, status and store in a json
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

def get_psm_metrics(psm_ip, sid, kind):
   # Grab the metrics from PSM
   headers = {
      'Cookie': 'sid=' + sid,
      'Content-Type': 'application/json'
   }

   url = 'https://'+ psm_ip +'/telemetry/v1/metrics'
   body = '{"queries":[{"kind":"' + kind + '","start-time":"now() - 2m","end-time":"now()"}]}'
   get_metrics_api_request = send_api_request(url, headers, body, "cookies.txt", 'GET')

   return get_metrics_api_request


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


@app.route('/switch-metrics')
def switch_metrics():
    metrics = ""
    # Get the PSM Session ID
    sid = app.config['psm_sid']

    # Get Power Metrics from PSM and parse them
    power_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "PowerMetrics")['content']))["results"][0]["series"][0]["values"]

    # Print PowerMetrics in open api format
    for obj in power_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[5], app.config['switches'])

        metrics += 'PowerMetrics_Pin{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'PowerMetrics_Pout1{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'PowerMetrics_Pout2{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)

    # Get AsicTemperatureMetrics Metrics from Switches
    asictemp_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "AsicTemperatureMetrics")['content']))["results"][0]["series"][0]["values"]

    for obj in asictemp_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[5], app.config['switches'])
        #reporterID = obj[5]

        metrics += 'AsicTemp_DieTemperature{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'AsicTemp_HbmTemperature{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'AsicTemp_LocalTemperature{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)

    # Get Lif Metrics from PSM
    lif_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "LifMetrics")['content']))["results"][0]["series"][0]["values"]

    for obj in lif_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[27], app.config['switches'])
        #reporterID = obj[27]

        metrics += 'LifMetrics_RxBroadcastBytes{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'LifMetrics_RxBroadcastPackets{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'LifMetrics_RxDMAError{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'LifMetrics_RxDropBroadcastBytes{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'LifMetrics_RxDropBroadcastPackets{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'LifMetrics_RxDropMulticastBytes{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'LifMetrics_RxDropMulticastPackets{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'LifMetrics_RxDropUnicastBytes{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'LifMetrics_RxDropUnicastPackets{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'LifMetrics_RxMulticastBytes{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'LifMetrics_RxMulticastPackets{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'LifMetrics_RxUnicastBytes{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)
        metrics += 'LifMetrics_RxUnicastPackets{node="%s"} %d %d\n' % (reporterID, obj[13], psm_date)
        metrics += 'LifMetrics_TxBroadcastBytes{node="%s"} %d %d\n' % (reporterID, obj[14], psm_date)
        metrics += 'LifMetrics_TxBroadcastPackets{node="%s"} %d %d\n' % (reporterID, obj[15], psm_date)
        metrics += 'LifMetrics_TxDropBroadcastBytes{node="%s"} %d %d\n' % (reporterID, obj[16], psm_date)
        metrics += 'LifMetrics_TxDropBroadcastPackets{node="%s"} %d %d\n' % (reporterID, obj[17], psm_date)
        metrics += 'LifMetrics_TxDropMulticastBytes{node="%s"} %d %d\n' % (reporterID, obj[18], psm_date)
        metrics += 'LifMetrics_TxDropMulticastPackets{node="%s"} %d %d\n' % (reporterID, obj[19], psm_date)
        metrics += 'LifMetrics_TxDropUnicastBytes{node="%s"} %d %d\n' % (reporterID, obj[20], psm_date)
        metrics += 'LifMetrics_TxDropUnicastPackets{node="%s"} %d %d\n' % (reporterID, obj[21], psm_date)
        metrics += 'LifMetrics_TxMulticastBytes{node="%s"} %d %d\n' % (reporterID, obj[22], psm_date)
        metrics += 'LifMetrics_TxMulticastPackets{node="%s"} %d %d\n' % (reporterID, obj[23], psm_date)
        metrics += 'LifMetrics_TxUnicastBytes{node="%s"} %d %d\n' % (reporterID, obj[24], psm_date)
        metrics += 'LifMetrics_TxUnicastPackets{node="%s"} %d %d\n' % (reporterID, obj[25], psm_date)

    # Get EgressDrops Metrics from PSM
    egressdrops_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "EgressDrops")['content']))["results"][0]["series"][0]["values"]

    for obj in egressdrops_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[16], app.config['switches'])
        #reporterID = obj[16]

        metrics += 'EgressDrops_CoPPDrops{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'EgressDrops_FlowepochmismatchCoPPdrops{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'EgressDrops_FlowmissCoPPdrops{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'EgressDrops_ForwardingDrops{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'EgressDrops_InvalidMirrorSessionDrops{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'EgressDrops_InvalidSessionDrops{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'EgressDrops_MpuExceptionDrops{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'EgressDrops_ParserErrorDrops{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'EgressDrops_PipelinePacketLoopDrops{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'EgressDrops_PolicyDrops{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'EgressDrops_RXPolicerDrops{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'EgressDrops_TxPolicerDrops{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)
        metrics += 'EgressDrops_UnexpectedSessionStateDrops{node="%s"} %d %d\n' % (reporterID, obj[13], psm_date)
        metrics += 'EgressDrops_VmotionTransientDrops{node="%s"} %d %d\n' % (reporterID, obj[14], psm_date)

    # Get IngressDrops Metrics from PSM
    ingressdrops_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "IngressDrops")['content']))["results"][0]["series"][0]["values"]

    for obj in ingressdrops_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[16], app.config['switches'])
        #reporterID = obj[16]

        metrics += 'IngressDrops_CoPPDrops{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'IngressDrops_FlowepochmismatchCoPPdrops{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'IngressDrops_FlowmissCoPPdrops{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'IngressDrops_ForwardingDrops{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'IngressDrops_InvalidMirrorSessionDrops{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'IngressDrops_InvalidSessionDrops{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'IngressDrops_MpuExceptionDrops{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'IngressDrops_ParserErrorDrops{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'IngressDrops_PipelinePacketLoopDrops{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'IngressDrops_PolicyDrops{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'IngressDrops_RXPolicerDrops{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'IngressDrops_TxPolicerDrops{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)
        metrics += 'IngressDrops_UnexpectedSessionStateDrops{node="%s"} %d %d\n' % (reporterID, obj[13], psm_date)
        metrics += 'IngressDrops_VmotionTransientDrops{node="%s"} %d %d\n' % (reporterID, obj[14], psm_date)

    # Get IngressDrops Metrics from PSM
    flowstatssummary_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "FlowStatsSummary")['content']))["results"][0]["series"][0]["values"]

    for obj in flowstatssummary_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[25], app.config['switches'])
        #reporterID = obj[25]

        metrics += 'FlowStatsSummary_ConnTrackDisabledSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'FlowStatsSummary_ConnTrackDisabledSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'FlowStatsSummary_DeletedConnTrackDisabledSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'FlowStatsSummary_DeletedConnTrackDisabledSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'FlowStatsSummary_DeletedICMPSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'FlowStatsSummary_DeletedICMPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'FlowStatsSummary_DeletedL2Sessions{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'FlowStatsSummary_DeletedOtherSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'FlowStatsSummary_DeletedOtherSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'FlowStatsSummary_DeletedTCPSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'FlowStatsSummary_DeletedTCPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'FlowStatsSummary_DeletedUDPSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)
        metrics += 'FlowStatsSummary_DeletedUDPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[13], psm_date)
        metrics += 'FlowStatsSummary_ICMPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[14], psm_date)
        metrics += 'FlowStatsSummary_DeletedL2Sessions{node="%s"} %d %d\n' % (reporterID, obj[15], psm_date)
        metrics += 'FlowStatsSummary_L2Sessions{node="%s"} %d %d\n' % (reporterID, obj[16], psm_date)
        metrics += 'FlowStatsSummary_OtherSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[17], psm_date)
        metrics += 'FlowStatsSummary_OtherSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[18], psm_date)
        metrics += 'FlowStatsSummary_SessionCreateErrors{node="%s"} %d %d\n' % (reporterID, obj[19], psm_date)
        metrics += 'FlowStatsSummary_TCPSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[20], psm_date)
        metrics += 'FlowStatsSummary_TCPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[21], psm_date)
        metrics += 'FlowStatsSummary_UDPSessionsOverIPv4{node="%s"} %d %d\n' % (reporterID, obj[22], psm_date)
        metrics += 'FlowStatsSummary_UDPSessionsOverIPv6{node="%s"} %d %d\n' % (reporterID, obj[23], psm_date)

    # Get DataPathAssistStats Metrics from PSM
    DataPathAssistStats_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "DataPathAssistStats")['content']))["results"][0]["series"][0]["values"]

    for obj in DataPathAssistStats_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[30], app.config['switches'])
        #reporterID = obj[25]

        metrics += 'DataPathAssistStats_ALGFlowRefreshFailure{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'DataPathAssistStats_ALGFlowSyncReceiveFailed{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'DataPathAssistStats_ALGFlowSyncSendFailed{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'DataPathAssistStats_ARPDrops{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'DataPathAssistStats_ARPPktsRx{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'DataPathAssistStats_ARPRepliesTx{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'DataPathAssistStats_DHCPDrops{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'DataPathAssistStats_DHCPPktsRx{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'DataPathAssistStats_DHCPPktsTx2ProxyServer{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'DataPathAssistStats_DHCPPktsTx2RelayClient{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'DataPathAssistStats_DHCPPktsTx2RelayServer{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'DataPathAssistStats_DNSFailure{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)
        metrics += 'DataPathAssistStats_FTPFailure{node="%s"} %d %d\n' % (reporterID, obj[13], psm_date)
        metrics += 'DataPathAssistStats_FlowDeleteFailure{node="%s"} %d %d\n' % (reporterID, obj[14], psm_date)
        metrics += 'DataPathAssistStats_FlowInstallFailure{node="%s"} %d %d\n' % (reporterID, obj[15], psm_date)
        metrics += 'DataPathAssistStats_FlowSyncFailure{node="%s"} %d %d\n' % (reporterID, obj[16], psm_date)
        metrics += 'DataPathAssistStats_FlowUpdateFailure{node="%s"} %d %d\n' % (reporterID, obj[17], psm_date)
        metrics += 'DataPathAssistStats_MSRPCFailure{node="%s"} %d %d\n' % (reporterID, obj[18], psm_date)
        metrics += 'DataPathAssistStats_NonSynTCPPktDrops{node="%s"} %d %d\n' % (reporterID, obj[19], psm_date)
        metrics += 'DataPathAssistStats_RTSPFailure{node="%s"} %d %d\n' % (reporterID, obj[20], psm_date)
        metrics += 'DataPathAssistStats_SunRPCFailure{node="%s"} %d %d\n' % (reporterID, obj[21], psm_date)
        metrics += 'DataPathAssistStats_TFTPFailure{node="%s"} %d %d\n' % (reporterID, obj[22], psm_date)
        metrics += 'DataPathAssistStats_TcpConnTrackFailure{node="%s"} %d %d\n' % (reporterID, obj[23], psm_date)
        metrics += 'DataPathAssistStats_TotalDrops{node="%s"} %d %d\n' % (reporterID, obj[24], psm_date)
        metrics += 'DataPathAssistStats_TotalPktsRx{node="%s"} %d %d\n' % (reporterID, obj[25], psm_date)
        metrics += 'DataPathAssistStats_TotalSessionsAged{node="%s"} %d %d\n' % (reporterID, obj[26], psm_date)
        metrics += 'DataPathAssistStats_TotalSessionsLearned{node="%s"} %d %d\n' % (reporterID, obj[27], psm_date)
        metrics += 'DataPathAssistStats_UnknownNetwork{node="%s"} %d %d\n' % (reporterID, obj[28], psm_date)

    # Get VnicDrops Metrics from PSM
    VnicDrops_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "VnicDrops")['content']))["results"][0]["series"][0]["values"]

    for obj in VnicDrops_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[5], app.config['switches'])
        #reporterID = obj[25]

        if obj[1] == None:
            VnicDrops_PolicyDrops = 0
        else:
            VnicDrops_PolicyDrops = obj[1]
        if obj[2] == None:
            VnicDrops_UnexpectedSessionStateDrops = 0
        else:
            VnicDrops_UnexpectedSessionStateDrops = obj[2]
        if obj[3] == None:
            VnicDrops_VmotionTransientDrops = 0
        else:
            VnicDrops_VmotionTransientDrops = obj[3]

        metrics += 'VnicDrops_PolicyDrops{node="%s"} %s %d\n' % (reporterID, VnicDrops_PolicyDrops, psm_date)
        metrics += 'VnicDrops_UnexpectedSessionStateDrops{node="%s"} %s %d\n' % (reporterID, VnicDrops_UnexpectedSessionStateDrops, psm_date)
        metrics += 'VnicDrops_VmotionTransientDrops{node="%s"} %s %d\n' % (reporterID, VnicDrops_VmotionTransientDrops, psm_date)

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
            switch_health = 1
        else:
            switch_health = 0
        dsc_version = obj["status"]["DSCVersion"]
        reporterID = get_reporter_id(obj["status"]["primary-mac"], app.config['switches'])
        #reporterID = obj["status"]["primary-mac"]
        switch_version = obj["status"]["dss-info"]["version"]

        metrics += 'DSC_health_status{node="%s" health_status="%s"} %s\n' % (reporterID, switch_health, switch_health)
        metrics += 'DSC_dsc_version{node="%s" dsc_version="%s"} 1\n' % (reporterID, dsc_version)
        metrics += 'DSC_switch_version{node="%s" switch_version="%s"} 1\n' % (reporterID, switch_version)
        metrics += 'DSC_node{node="%s" switch_version="%s"} 1\n' % (reporterID, switch_version)
        metrics += 'DSC_node{node="%s" health_status="%s" dsc_version="%s" switch_version="%s"} 1\n' % (reporterID, switch_health, dsc_version, switch_version) 

    # Try to find out if ELBA is enabled
    url = 'https://'+ app.config['psm_ip'] +'/configs/network/v1/tenant/default/networks'
    headers = {
        'Cookie': 'sid=' + app.config['psm_sid'],
        'Content-Type': 'application/json'
    }

    body = '{"queries":[{"kind":"NetworkList","start-time":"now() - 2m","end-time":"now()"}]}'

    network_objects = (json.loads(send_api_request(url, headers, body, cookie_file, 'GET')['content']))['items']

    elba_enabled = 0
    network_counter = 0

    for obj in network_objects:
        if obj['kind'] == 'Network':
            network_counter = network_counter + 1
            if 'service-bypass' in obj['spec']:
                if obj['spec']['service-bypass'] == False:
                    elba_enabled = 1
                    break
                    #print('bypass=%s, name=%s' % (obj['spec']['service-bypass'], obj['meta']['name']))
                else:
                    elba_enabled = 1
                    #print('bypass=True, name=%s' % (obj['meta']['name']))

    metrics += 'DSC_ELBA_enabled{elba_enabled="%s"} %s\n' % (elba_enabled, elba_enabled)
    metrics += 'DSC_Number_of_networks{num_networks="%d"} %d\n' % (network_counter, network_counter)

    return metrics

@app.route('/psm-metrics')
def psm_metrics():
    metrics = ""
    # Get the PSM Session ID
    sid = app.config['psm_sid']

    # Get Cluster Metrics from PSM
    cluster_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "Cluster")['content']))["results"][0]["series"][0]["values"]

    for obj in cluster_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[9], app.config['switches'])
        #reporterID = obj[9]

        metrics += 'Cluster_AdmittedNICs{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'Cluster_DecommissionedNICs{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'Cluster_DisconnectedNICs{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'Cluster_HealthyNICs{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'Cluster_PendingNICs{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'Cluster_RejectedNICs{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'Cluster_UnhealthyNICs{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)

    # Get Node Metrics from PSM and parse them
    node_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "Node")['content']))["results"][0]["series"][0]["values"]

    # Print NodeMetrics in open api format
    for obj in node_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[14], app.config['switches'])
        #reporterID = obj[14]

        metrics += 'Node_CPUUsedPercent{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'Node_DiskFree{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'Node_DiskTotal{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)
        metrics += 'Node_DiskUsed{node="%s"} %d %d\n' % (reporterID, obj[4], psm_date)
        metrics += 'Node_DiskUsedPercent{node="%s"} %d %d\n' % (reporterID, obj[5], psm_date)
        metrics += 'Node_InterfaceRxBytes{node="%s"} %d %d\n' % (reporterID, obj[6], psm_date)
        metrics += 'Node_InterfaceTxBytes{node="%s"} %d %d\n' % (reporterID, obj[7], psm_date)
        metrics += 'Node_MemAvailable{node="%s"} %d %d\n' % (reporterID, obj[8], psm_date)
        metrics += 'Node_MemFree{node="%s"} %d %d\n' % (reporterID, obj[9], psm_date)
        metrics += 'Node_MemTotal{node="%s"} %d %d\n' % (reporterID, obj[10], psm_date)
        metrics += 'Node_MemUsed{node="%s"} %d %d\n' % (reporterID, obj[11], psm_date)
        metrics += 'Node_MemUsedPercent{node="%s"} %d %d\n' % (reporterID, obj[12], psm_date)

    # Get MemoryMetrics Metrics from PSM
    memory_metrics_json_objects = (json.loads(get_psm_metrics(app.config['psm_ip'], sid, "MemoryMetrics")['content']))["results"][0]["series"][0]["values"]

    for obj in memory_metrics_json_objects:
        psm_date = convert_time(obj[0])
        reporterID = get_reporter_id(obj[5], app.config['switches'])
        #reporterID = obj[5]

        metrics += 'MemoryMetrics_Availablememory{node="%s"} %d %d\n' % (reporterID, obj[1], psm_date)
        metrics += 'MemoryMetrics_Freememory{node="%s"} %d %d\n' % (reporterID, obj[2], psm_date)
        metrics += 'MemoryMetrics_Totalmemory{node="%s"} %d %d\n' % (reporterID, obj[3], psm_date)

    return metrics





if __name__ == '__main__':
    app.run(debug=True, port=5001)

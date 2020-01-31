import datetime
import gzip
import json
import logging
import math
import os
import tempfile
import time

import boto3
import botocore

logger = logging.getLogger()
logger.setLevel(logging.INFO)

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
IP_SET_ID_MANUAL_BLOCK = os.environ["IP_SET_ID_MANUAL_BLOCK"]
IP_SET_ID_AUTO_BLOCK = os.environ["IP_SET_ID_AUTO_BLOCK"]

BLACKLIST_BLOCK_PERIOD = os.environ["BLACKLIST_BLOCK_PERIOD"]  # in minutes
REQUEST_PER_MINUTE_LIMIT = os.environ["REQUEST_PER_MINUTE_LIMIT"]

BLOCK_ERROR_CODES = ["400", "403", "404", "405"]  # error codes to parse logs for
LIMIT_IP_ADDRESS_RANGES_PER_IP_MATCH_CONDITION = 1000
API_CALL_NUM_RETRIES = 3
OUTPUT_FILE_KEY = "AWS/WAF/current_outstanding_requesters.json"
TEMP_DIR = tempfile.gettempdir()

CW = boto3.client("cloudwatch")
S3 = boto3.client("s3")
S3_RESOURCE = boto3.resource('s3')
WAF = boto3.client("waf")

LINE_FORMAT = {"date": 0, "time": 1, "source_ip": 4, "code": 8}


def get_outstanding_requesters(bucket_name, key_name):
    outstanding_requesters = {"block": {}}
    result = {}
    num_requests = 0
    local_file_path = f'{TEMP_DIR}/{key_name.split("/")[-1]}'
    S3.download_file(bucket_name, key_name, local_file_path)

    with gzip.open(local_file_path, "rt") as content:
        for line in content:
            try:
                line_data = line.split("\t")
                if line_data[LINE_FORMAT["code"]] in BLOCK_ERROR_CODES:
                    request_key = line_data[LINE_FORMAT["date"]]
                    request_key += "-" + line_data[LINE_FORMAT["time"]][:-3]
                    request_key += "-" + line_data[LINE_FORMAT["source_ip"]]
                    if request_key in result.keys():
                        result[request_key] += 1
                    else:
                        result[request_key] = 1
            except IndexError:
                pass

            num_requests += 1

    now_timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k, v in result.items():
        k = k.split("-")[-1]
        if v > REQUEST_PER_MINUTE_LIMIT:
            if k not in outstanding_requesters["block"].keys() or outstanding_requesters["block"][k] < v:
                outstanding_requesters["block"][k] = {
                    "max_req_per_min": v,
                    "updated_at": now_timestamp_str,
                }

    return outstanding_requesters, num_requests


def merge_current_blocked_requesters(key_name, outstanding_requesters):
    now_timestamp = datetime.datetime.now()
    now_timestamp_str = now_timestamp.strftime("%Y-%m-%d %H:%M:%S")

    local_file_path = f'{TEMP_DIR}/{key_name.split("/")[-1]}_LOCAL.json'
    S3.download_file(OUTPUT_BUCKET, OUTPUT_FILE_KEY, local_file_path)

    with open(local_file_path, "r") as file_content:
        content = file_content.read()
        remote_outstanding_requesters = json.loads(content)
    for k, v in remote_outstanding_requesters["block"].items():
        if v["max_req_per_min"] > REQUEST_PER_MINUTE_LIMIT:
            if k in outstanding_requesters["block"].keys():
                max_v = v["max_req_per_min"]
                if outstanding_requesters["block"][k]["max_req_per_min"] > max_v:
                    max_v = outstanding_requesters["block"][k]["max_req_per_min"]
                outstanding_requesters["block"][k] = {
                    "max_req_per_min": max_v,
                    "updated_at": now_timestamp_str,
                }
            else:
                prev_updated_at = datetime.datetime.strptime(v["updated_at"], "%Y-%m-%d %H:%M:%S")
                total_diff_min = ((now_timestamp - prev_updated_at).total_seconds()) / 60
                if total_diff_min > (BLACKLIST_BLOCK_PERIOD):
                    outstanding_requesters["block"][k] = v

    return outstanding_requesters


def write_output(key_name, outstanding_requesters):
    current_data = f'{TEMP_DIR}/{key_name.split("/")[-1]}_LOCAL.json'
    with open(current_data, "w") as outfile:
        json.dump(outstanding_requesters, outfile)
    S3_RESOURCE.meta.client.upload_file(current_data, OUTPUT_BUCKET, OUTPUT_FILE_KEY)


def waf_get_ip_set(ip_set_id):
    response = None

    for attempt in range(API_CALL_NUM_RETRIES):
        try:
            response = WAF.get_ip_set(IPSetId=ip_set_id)
        except botocore.exceptions.ParamValidationError as e:
            logger.info(e)
            delay = math.pow(2, attempt)
            time.sleep(delay)

    return response


def waf_update_ip_set(ip_set_id, updates_list):
    response = None

    if not updates_list:
        for attempt in range(API_CALL_NUM_RETRIES):
            try:
                response = WAF.update_ip_set(
                    IPSetId=ip_set_id, ChangeToken=WAF.get_change_token()["ChangeToken"], Updates=updates_list,
                )
            except botocore.exceptions.ParamValidationError as e:
                logger.info(e)
                delay = math.pow(2, attempt)
                time.sleep(delay)
    return response


def get_ip_set_already_blocked():
    ip_set_already_blocked = []

    if IP_SET_ID_MANUAL_BLOCK is not None:
        response = waf_get_ip_set(IP_SET_ID_MANUAL_BLOCK)
        if response is not None:
            for k in response["IPSet"]["IPSetDescriptors"]:
                ip_set_already_blocked.append(k["Value"])

    return ip_set_already_blocked


def is_already_blocked(ip, ip_set):
    result = False

    for net in ip_set:
        ipaddr = int("".join(["%02x" % int(x) for x in ip.split(".")]), 16)
        netstr, bits = net.split("/")
        netaddr = int("".join(["%02x" % int(x) for x in netstr.split(".")]), 16)
        mask = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF

        if (ipaddr & mask) == (netaddr & mask):
            result = True
            break

    return result


def update_waf_ip_set(outstanding_requesters, ip_set_id, ip_set_already_blocked):
    counter = 0

    if ip_set_id is None:
        return {"StatusCode": 200}

    updates_list = []
    top_outstanding_requesters = {}

    for key, value in sorted(outstanding_requesters.items(), key=lambda kv: kv[1]["max_req_per_min"], reverse=True, ):
        if counter < LIMIT_IP_ADDRESS_RANGES_PER_IP_MATCH_CONDITION:
            if not is_already_blocked(key, ip_set_already_blocked):
                top_outstanding_requesters[key] = value
                counter += 1
        else:
            break

    response = waf_get_ip_set(ip_set_id)
    if response is not None:
        for k in response["IPSet"]["IPSetDescriptors"]:
            ip_value = k["Value"].split("/")[0]
            if ip_value not in top_outstanding_requesters.keys():
                updates_list.append(
                    {"Action": "DELETE", "IPSetDescriptor": {"Type": "IPV4", "Value": k["Value"]}, }
                )
            else:
                # Dont block an already blocked IP
                top_outstanding_requesters.pop(ip_value, None)

    for k in top_outstanding_requesters.keys():
        updates_list.append(
            {"Action": "INSERT", "IPSetDescriptor": {"Type": "IPV4", "Value": "%s/32" % k}, }
        )

    waf_update_ip_set(ip_set_id, updates_list)

    return counter


def handler(event, context):
    bucket_name = event["Records"][0]["s3"]["bucket"]["name"]
    key_name = event["Records"][0]["s3"]["object"]["key"]

    if key_name == OUTPUT_FILE_KEY:
        return {"StatusCode": 200}

    outstanding_requesters, num_requests = get_outstanding_requesters(bucket_name, key_name)
    outstanding_requesters = merge_current_blocked_requesters(key_name, outstanding_requesters)

    write_output(key_name, outstanding_requesters)

    ip_set_already_blocked = get_ip_set_already_blocked()
    num_blocked = update_waf_ip_set(outstanding_requesters["block"], IP_SET_ID_AUTO_BLOCK, ip_set_already_blocked)

    CW.put_metric_data(
        Namespace=f"WAFReactiveBlacklist-{OUTPUT_BUCKET}",
        MetricData=[
            {"MetricName": "IPBlocked", "Timestamp": datetime.datetime.now(), "Value": num_blocked, "Unit": "Count", },
            {
                "MetricName": "NumRequests",
                "Timestamp": datetime.datetime.now(),
                "Value": num_requests,
                "Unit": "Count",
            },
        ],
    )

    return outstanding_requesters

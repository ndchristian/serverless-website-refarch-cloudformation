# Copyright 2020 Nicholas Christian

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#    http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def default(o):
    """Allows for displaying the datetime object in get_health_check_status response"""
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()


def get_all_health_checks(client):
    """Gets all health check objects incase the 100 result limit is reached"""
    health_checks = []
    list_health_checks_args = {}
    continue_list_health_checks_args = True
    while continue_list_health_checks_args:
        list_health_checks_response = client.list_health_checks(**list_health_checks_args)
        for health_check in list_health_checks_response["HealthChecks"]:
            health_checks.append(health_check)
        if list_health_checks_response["IsTruncated"]:
            list_health_checks_args.update({"Marker": list_health_checks_response["NextMarker"]})
        else:
            continue_list_health_checks_args = False
    return health_checks


def handler(event, context):
    """Checks in a change of status of a health check and alerts through SNS"""
    route53 = boto3.client("route53")
    sns = boto3.client("sns")
    ssm = boto3.client("ssm")

    health_checks = get_all_health_checks(client=route53)
    for health_check in health_checks:
        tags = {
            tag["Key"]: tag["Value"]
            for tag in route53.list_tags_for_resource(ResourceType="healthcheck", ResourceId=health_check["Id"])[
                "ResourceTagSet"
            ]["Tags"]
        }
        if os.environ["ENVIRONMENT"] == tags["Environment"] and os.environ["APPLICATION"] == tags["Application"]:
            health_check_info = route53.get_health_check(HealthCheckId=health_check["Id"])
            logger.info(health_check["Id"])
            current_status = route53.get_health_check_status(HealthCheckId=health_check["Id"])[
                "HealthCheckObservations"
            ]
            last_failures = route53.get_health_check_last_failure_reason(HealthCheckId=health_check["Id"])[
                "HealthCheckObservations"
            ]

            current_status_times = [times["StatusReport"]["CheckedTime"] for times in current_status]
            last_failures_times = [times["StatusReport"]["CheckedTime"] for times in last_failures]

            try:
                last_known_status = ssm.get_parameter(
                    Name="/{}/{}/{}".format(
                        os.environ["APPLICATION"],
                        os.environ["ENVIRONMENT"],
                        health_check_info["HealthCheck"]["HealthCheckConfig"]["FullyQualifiedDomainName"],
                    )
                )["Parameter"]["Value"]
            except ClientError as e:
                logger.info(e.response["Error"]["Code"])
                last_known_status = "UNKNOWN"

            if not set(current_status_times).isdisjoint(last_failures_times) and last_known_status in [
                "OKAY",
                "UNKNOWN",
            ]:

                message = "{} has failed. Please visit https://console.aws.amazon.com/route53/healthchecks/home?region=us-east-1#/ for more information.".format(
                    health_check_info["HealthCheck"]["HealthCheckConfig"]["FullyQualifiedDomainName"]
                )

                sns.publish(
                    TopicArn=os.environ["SNS_TOPIC"],
                    Message=message,
                    Subject="Website node in {} has failed".format(os.environ["ENVIRONMENT"]),
                )

                ssm.put_parameter(
                    Name="/{}/{}/{}".format(
                        os.environ["APPLICATION"],
                        os.environ["ENVIRONMENT"],
                        health_check_info["HealthCheck"]["HealthCheckConfig"]["FullyQualifiedDomainName"],
                    ),
                    Description="Stores state for %s serverless website health checks" % os.environ["APPLICATION"],
                    Value="FAILED",
                    Type="String",
                    Overwrite=True,
                )
                logger.info(message)

            if set(current_status_times).isdisjoint(last_failures_times) and last_known_status == "FAILED":
                message = "{} is back up. Please visit https://console.aws.amazon.com/route53/healthchecks/home?region=us-east-1#/ for more information.".format(
                    health_check_info["HealthCheck"]["HealthCheckConfig"]["FullyQualifiedDomainName"]
                )

                sns.publish(
                    TopicArn=os.environ["SNS_TOPIC"],
                    Message=message,
                    Subject="Website node in {} is back up".format(os.environ["ENVIRONMENT"]),
                )

                ssm.put_parameter(
                    Name="/{}/{}/{}".format(
                        os.environ["APPLICATION"],
                        os.environ["ENVIRONMENT"],
                        health_check_info["HealthCheck"]["HealthCheckConfig"]["FullyQualifiedDomainName"],
                    ),
                    Description="Stores state for %s serverless website health checks" % os.environ["APPLICATION"],
                    Value="OKAY",
                    Type="String",
                    Overwrite=True,
                )

                logger.info(message)

    return {"StatusCode": 200}
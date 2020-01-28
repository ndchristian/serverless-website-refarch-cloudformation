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

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """Copies objects fom the master bucket to each node bucket"""

    s3 = boto3.resource("s3")
    s3_client = boto3.client("s3")

    for bucket in s3_client.list_buckets()["Buckets"]:
        if bucket["Name"].startswith(os.environ["FILTERPREFIX"]) and bucket["Name"].endswith(os.environ["ENVIRONMENT"]):
            copy_source = {
                "Bucket": str(event["Records"][0]["s3"]["bucket"]["name"]),
                "Key": str(event["Records"][0]["s3"]["object"]["key"]),
            }
            s3.meta.client.copy(copy_source, bucket["Name"], event["Records"][0]["s3"]["object"]["key"])
            logger.info("Copied {} to {}".format(event["Records"][0]["s3"]["object"]["key"], bucket["Name"]))
    return {"statusCode": 200}

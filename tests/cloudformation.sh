#!/usr/bin/env bash

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

# REQUIREMENTS

# https://github.com/aws-cloudformation/cfn-python-lint
# https://github.com/stelligent/cfn_nag

for file in "$PWD"/../cloudformation/*
do
 	cfn-lint "$file" 
 	cfn_nag_scan -i "$file"
done
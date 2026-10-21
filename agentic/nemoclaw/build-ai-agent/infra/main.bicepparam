// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Local authoring/rehearsal inputs. Skillable maps parameters in its UI instead.
// Blank inference URL/key discover this instance's Foundry connection. No keys are committed.
using './main.bicep'

param namePrefix = readEnvironmentVariable('IGNITE_LAB_NAME_PREFIX', 'ignite-agent')
param labInstanceId = readEnvironmentVariable('IGNITE_LAB_INSTANCE_ID', 'development')
param vmAdminUsername = readEnvironmentVariable('IGNITE_LAB_VM_USERNAME', 'labuser')
param vmAdminPassword = readEnvironmentVariable('IGNITE_LAB_VM_PASSWORD')
param inferenceEndpoint = readEnvironmentVariable('IGNITE_LAB_INFERENCE_ENDPOINT', '')
param inferenceModel = readEnvironmentVariable('IGNITE_LAB_INFERENCE_MODEL', '')
param foundryAccountName = readEnvironmentVariable('IGNITE_LAB_FOUNDRY_ACCOUNT_NAME', 'myfoundry${labInstanceId}')
param inferenceApiKey = readEnvironmentVariable('IGNITE_LAB_INFERENCE_API_KEY', '')
param inferenceAuthHeader = readEnvironmentVariable('IGNITE_LAB_INFERENCE_AUTH_HEADER', 'Authorization')
param deployLabSetup = bool(readEnvironmentVariable('IGNITE_LAB_DEPLOY_SETUP', 'true'))
// Keep false for the learner installation exercise; true is an author recovery mode.
param preinstallAgent = bool(readEnvironmentVariable('IGNITE_LAB_PREINSTALL_AGENT', 'false'))
param setupRevision = readEnvironmentVariable('IGNITE_LAB_SETUP_REVISION', '1')
param vmImageResourceId = readEnvironmentVariable('IGNITE_LAB_VM_IMAGE_ID', '')
param bootstrapPrincipalObjectId = readEnvironmentVariable('IGNITE_LAB_BOOTSTRAP_OBJECT_ID', '')
// Leave unset to start with SSH closed; set a single IPv4 address for an
// authorized rehearsal or use the Cloud Shell connection helper after launch.
param sshSourceIPv4 = readEnvironmentVariable('IGNITE_LAB_SSH_SOURCE_IPV4', '')
// Opt in only when the lab's access design accepts SSH from all Azure public
// IP ranges. This removes the need for a learner-run firewall helper.
param allowAzureCloudSsh = bool(readEnvironmentVariable('IGNITE_LAB_ALLOW_AZURE_CLOUD_SSH', 'false'))

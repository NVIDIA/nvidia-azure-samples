#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCATION="${LOCATION:-swedencentral}"
readonly RESOURCE_GROUP="${RESOURCE_GROUP:-rg-nemotron-asr-aca}"
readonly NAME_PREFIX="${NAME_PREFIX:-nemotron-asr}"
readonly WORKLOAD_PROFILE_TYPE="${WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC24-A100}"
readonly DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-nemotron-asr}"
readonly TARGET_IMAGE="${TARGET_IMAGE:-nvidia/nemotron-asr-streaming:amd64}"
readonly SOURCE_IMAGE="${SOURCE_IMAGE:-nvcr.io/nim/nvidia/nemotron-asr-streaming@sha256:044e60d13b12bf79efdb2aac0d88b3eaae766aba934a311b13962a2b4b511759}"
readonly EXTERNAL_INGRESS="${EXTERNAL_INGRESS:-false}"
readonly ALLOWED_IP_CIDR="${ALLOWED_IP_CIDR:-}"
readonly MIN_REPLICAS="${MIN_REPLICAS:-1}"
readonly ENABLE_ARTIFACT_STREAMING="${ENABLE_ARTIFACT_STREAMING:-false}"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

command -v az >/dev/null 2>&1 || fail "Azure CLI is required."
az account show --output none >/dev/null 2>&1 || fail "Run 'az login' before deploying."

[[ -n "${NGC_API_KEY:-}" ]] || fail "Set NGC_API_KEY to an NGC personal API key."
[[ "$NGC_API_KEY" != *'<'* ]] || fail "NGC_API_KEY still contains a placeholder."
[[ "${NVIDIA_AI_ENTERPRISE_LICENSE_ACCEPTED:-false}" == "true" ]] || \
  fail "Confirm the NVIDIA AI Enterprise self-hosting license by setting NVIDIA_AI_ENTERPRISE_LICENSE_ACCEPTED=true."
[[ "${ACA_GPU_QUOTA_CONFIRMED:-false}" == "true" ]] || \
  fail "Confirm serverless A100 quota in the target region by setting ACA_GPU_QUOTA_CONFIRMED=true."

if [[ "$EXTERNAL_INGRESS" == "true" && -z "$ALLOWED_IP_CIDR" ]]; then
  fail "External ingress requires ALLOWED_IP_CIDR, for example <PUBLIC_EGRESS_IPV4>/32."
fi

printf 'Checking %s availability in %s...\n' "$WORKLOAD_PROFILE_TYPE" "$LOCATION"
supported_profile="$(
  az containerapp env workload-profile list-supported \
    --location "$LOCATION" \
    --query "[?name=='${WORKLOAD_PROFILE_TYPE}'].name | [0]" \
    --output tsv
)"
[[ "$supported_profile" == "$WORKLOAD_PROFILE_TYPE" ]] || \
  fail "$WORKLOAD_PROFILE_TYPE is not available in $LOCATION."

printf 'Current Container Apps quota information for %s:\n' "$LOCATION"
az containerapp list-usages \
  --location "$LOCATION" \
  --query "value[].{name:name.localizedValue,current:currentValue,limit:limit}" \
  --output table
printf 'Verify that your subscription has serverless A100 quota before continuing.\n'

for provider in Microsoft.App Microsoft.ContainerRegistry Microsoft.KeyVault Microsoft.ManagedIdentity; do
  printf 'Registering resource provider %s...\n' "$provider"
  az provider register --namespace "$provider" --wait --output none
done

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags application=nemotron-asr-streaming environment=demo managedBy=bicep \
  --output none

printf 'Deploying shared infrastructure...\n'
az deployment group create \
  --name "${DEPLOYMENT_NAME}-infra" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters \
    location="$LOCATION" \
    namePrefix="$NAME_PREFIX" \
    ngcApiKey="$NGC_API_KEY" \
    workloadProfileType="$WORKLOAD_PROFILE_TYPE" \
  --output none

deployment_output() {
  az deployment group show \
    --name "${DEPLOYMENT_NAME}-infra" \
    --resource-group "$RESOURCE_GROUP" \
    --query "properties.outputs.$1.value" \
    --output tsv
}

readonly ACR_NAME="$(deployment_output acrName)"
readonly ACR_LOGIN_SERVER="$(deployment_output acrLoginServer)"
readonly ACA_ENVIRONMENT_NAME="$(deployment_output containerAppsEnvironmentName)"
readonly IDENTITY_NAME="$(deployment_output identityName)"
readonly NGC_SECRET_URI="$(deployment_output ngcSecretUri)"
readonly WORKLOAD_PROFILE_NAME="$(deployment_output workloadProfileName)"

printf 'Importing the digest-pinned NIM image into %s...\n' "$ACR_NAME"
az acr import \
  --name "$ACR_NAME" \
  --source "$SOURCE_IMAGE" \
  --image "$TARGET_IMAGE" \
  --username '$oauthtoken' \
  --password "$NGC_API_KEY" \
  --force \
  --output none

if [[ "$ENABLE_ARTIFACT_STREAMING" == "true" ]]; then
  printf 'Enabling ACR artifact streaming preview for the imported image...\n'
  az acr artifact-streaming create \
    --name "$ACR_NAME" \
    --image "$TARGET_IMAGE" \
    --output none
fi

printf 'Deploying the Container App...\n'
az deployment group create \
  --name "${DEPLOYMENT_NAME}-app" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/app.bicep" \
  --parameters \
    location="$LOCATION" \
    containerAppsEnvironmentName="$ACA_ENVIRONMENT_NAME" \
    identityName="$IDENTITY_NAME" \
    acrLoginServer="$ACR_LOGIN_SERVER" \
    ngcSecretUri="$NGC_SECRET_URI" \
    workloadProfileName="$WORKLOAD_PROFILE_NAME" \
    targetImage="$TARGET_IMAGE" \
    externalIngress="$EXTERNAL_INGRESS" \
    allowedIpCidr="$ALLOWED_IP_CIDR" \
    minReplicas="$MIN_REPLICAS" \
  --output none

fqdn="$(
  az deployment group show \
    --name "${DEPLOYMENT_NAME}-app" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.fqdn.value \
    --output tsv
)"

printf 'Deployment complete.\n'
printf 'Container App FQDN: %s\n' "$fqdn"
if [[ "$EXTERNAL_INGRESS" == "true" ]]; then
  printf 'gRPC endpoint: %s:443\n' "$fqdn"
else
  printf 'Ingress is internal. Test from a client with access to the Container Apps environment.\n'
fi

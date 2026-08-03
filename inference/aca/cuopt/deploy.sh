#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCATION="${LOCATION:-eastus}"
readonly RESOURCE_GROUP="${RESOURCE_GROUP:-rg-freshroute-cuopt-aca}"
readonly NAME_PREFIX="${NAME_PREFIX:-freshroute-cuopt}"
readonly WORKLOAD_PROFILE_TYPE="${WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC24-A100}"
readonly DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-freshroute-cuopt}"
readonly CUOPT_TARGET_IMAGE="${CUOPT_TARGET_IMAGE:-nvidia/cuopt:26.6.0-cuda12.9-py3.14}"
readonly CUOPT_SOURCE_IMAGE="${CUOPT_SOURCE_IMAGE:-nvcr.io/nvidia/cuopt/cuopt@sha256:170215e65abde282a83005f640145e7b5c9af35525e95a096975bb3000f0f94c}"
readonly WEB_TARGET_IMAGE="${WEB_TARGET_IMAGE:-freshroute/web:1.0.0}"
readonly ALLOWED_IP_CIDR="${ALLOWED_IP_CIDR:-}"
readonly PUBLIC_DEMO_ACKNOWLEDGED="${PUBLIC_DEMO_ACKNOWLEDGED:-false}"
readonly CUOPT_MIN_REPLICAS="${CUOPT_MIN_REPLICAS:-0}"
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

if [[ -z "$ALLOWED_IP_CIDR" && "$PUBLIC_DEMO_ACKNOWLEDGED" != "true" ]]; then
  fail "Set ALLOWED_IP_CIDR (recommended) or explicitly set PUBLIC_DEMO_ACKNOWLEDGED=true."
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

for provider in Microsoft.App Microsoft.ContainerRegistry Microsoft.ManagedIdentity Microsoft.Maps; do
  printf 'Registering resource provider %s...\n' "$provider"
  az provider register --namespace "$provider" --wait --output none
done

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags application=freshroute-cuopt environment=demo managedBy=bicep \
  --output none

printf 'Deploying shared infrastructure...\n'
az deployment group create \
  --name "${DEPLOYMENT_NAME}-infra" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters location="$LOCATION" namePrefix="$NAME_PREFIX" workloadProfileType="$WORKLOAD_PROFILE_TYPE" \
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
readonly ACA_ENVIRONMENT_DOMAIN="$(deployment_output containerAppsEnvironmentDomain)"
readonly IDENTITY_NAME="$(deployment_output identityName)"
readonly IDENTITY_CLIENT_ID="$(deployment_output identityClientId)"
readonly MAPS_CLIENT_ID="$(deployment_output mapsClientId)"
readonly WORKLOAD_PROFILE_NAME="$(deployment_output workloadProfileName)"

printf 'Importing the digest-pinned cuOpt image into %s...\n' "$ACR_NAME"
az acr import \
  --name "$ACR_NAME" \
  --source "$CUOPT_SOURCE_IMAGE" \
  --image "$CUOPT_TARGET_IMAGE" \
  --username '$oauthtoken' \
  --password "$NGC_API_KEY" \
  --force \
  --output none

if [[ "$ENABLE_ARTIFACT_STREAMING" == "true" ]]; then
  printf 'Enabling ACR artifact streaming preview for cuOpt...\n'
  az acr artifact-streaming create \
    --name "$ACR_NAME" \
    --image "$CUOPT_TARGET_IMAGE" \
    --output none
fi

printf 'Building the FreshRoute web image in ACR...\n'
az acr build \
  --registry "$ACR_NAME" \
  --image "$WEB_TARGET_IMAGE" \
  "$SCRIPT_DIR"

printf 'Deploying cuOpt and the FreshRoute web application...\n'
az deployment group create \
  --name "${DEPLOYMENT_NAME}-apps" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/app.bicep" \
  --parameters \
    location="$LOCATION" \
    containerAppsEnvironmentName="$ACA_ENVIRONMENT_NAME" \
    containerAppsEnvironmentDomain="$ACA_ENVIRONMENT_DOMAIN" \
    identityName="$IDENTITY_NAME" \
    identityClientId="$IDENTITY_CLIENT_ID" \
    mapsClientId="$MAPS_CLIENT_ID" \
    acrLoginServer="$ACR_LOGIN_SERVER" \
    workloadProfileName="$WORKLOAD_PROFILE_NAME" \
    cuoptTargetImage="$CUOPT_TARGET_IMAGE" \
    webTargetImage="$WEB_TARGET_IMAGE" \
    allowedIpCidr="$ALLOWED_IP_CIDR" \
    cuoptMinReplicas="$CUOPT_MIN_REPLICAS" \
  --output none

web_fqdn="$(
  az deployment group show \
    --name "${DEPLOYMENT_NAME}-apps" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.webFqdn.value \
    --output tsv
)"

printf 'Deployment complete.\n'
printf 'FreshRoute URL: https://%s\n' "$web_fqdn"
printf 'cuOpt ingress is internal and its minimum replica count is %s.\n' "$CUOPT_MIN_REPLICAS"

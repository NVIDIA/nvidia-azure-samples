#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCATION="${LOCATION:-westeurope}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-alpamayo-nim}"
NAME_PREFIX="${NAME_PREFIX:-alpamayo-nim}"
CLUSTER_NAME="${CLUSTER_NAME:-}"

SYSTEM_NODE_VM_SIZE="${SYSTEM_NODE_VM_SIZE:-Standard_D8s_v5}"
SYSTEM_NODE_COUNT="${SYSTEM_NODE_COUNT:-1}"
GPU_NODEPOOL_NAME="${GPU_NODEPOOL_NAME:-gpunim}"
GPU_NODE_VM_SIZE="${GPU_NODE_VM_SIZE:-Standard_NC24ads_A100_v4}"
GPU_NODE_COUNT="${GPU_NODE_COUNT:-1}"
GPU_OS_DISK_SIZE_GB="${GPU_OS_DISK_SIZE_GB:-512}"
GPU_DRIVER="${GPU_DRIVER:-Install}"
GPU_MANAGEMENT_MODE="${GPU_MANAGEMENT_MODE:-Managed}"
GPU_MIG_STRATEGY="${GPU_MIG_STRATEGY:-None}"
TAINT_GPU_NODEPOOL="${TAINT_GPU_NODEPOOL:-false}"
ENABLE_AZURE_MONITOR="${ENABLE_AZURE_MONITOR:-true}"
GPU_QUOTA_FAMILY_NAME="${GPU_QUOTA_FAMILY_NAME:-StandardNCADSA100v4Family}"
GPU_REQUIRED_VCPUS="${GPU_REQUIRED_VCPUS:-24}"

NAMESPACE="${NAMESPACE:-alpamayo-nim}"
NIM_SERVICE_NAME="${NIM_SERVICE_NAME:-alpamayo-nim}"
NIM_CACHE_NAME="${NIM_CACHE_NAME:-$NIM_SERVICE_NAME}"
ALPAMAYO_NIM_REPOSITORY="nvcr.io/nim/nvidia/alpamayo-1-5-10b"
ALPAMAYO_NIM_TAG="1.0.0"
NIM_IMAGE_REPOSITORY="${NIM_IMAGE_REPOSITORY:-$ALPAMAYO_NIM_REPOSITORY}"
NIM_IMAGE_TAG="${NIM_IMAGE_TAG:-$ALPAMAYO_NIM_TAG}"
NIM_MODEL_PROFILE_ID="${NIM_MODEL_PROFILE_ID:-}"
NIM_ENGINE="${NIM_ENGINE:-}"
NIM_TENSOR_PARALLELISM="${NIM_TENSOR_PARALLELISM:-1}"
NIM_GPU_LIMIT="${NIM_GPU_LIMIT:-1}"
NIM_SERVICE_TYPE="${NIM_SERVICE_TYPE:-ClusterIP}"
NIM_SERVICE_PORT="${NIM_SERVICE_PORT:-8000}"
NIM_CACHE_SIZE="${NIM_CACHE_SIZE:-250Gi}"
NIM_STORAGE_CLASS="${NIM_STORAGE_CLASS:-managed-csi-premium}"
NIM_VOLUME_ACCESS_MODE="${NIM_VOLUME_ACCESS_MODE:-ReadWriteOnce}"
NIM_OPERATOR_VERSION="${NIM_OPERATOR_VERSION:-3.1.2}"
NIM_SERVER_DRY_RUN="${NIM_SERVER_DRY_RUN:-false}"
INSTALL_GPU_OPERATOR="${INSTALL_GPU_OPERATOR:-false}"
MANAGED_GPU_FEATURE_WAIT_SECONDS="${MANAGED_GPU_FEATURE_WAIT_SECONDS:-1800}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

require_true() {
  local name="$1"
  local value="${!name:-}"
  [ "$value" = "true" ] || fail "Set $name=true after reviewing the related quota/license requirement."
}

register_provider() {
  local namespace="$1"
  echo "Registering Azure provider: $namespace"
  az provider register --namespace "$namespace" --wait >/dev/null
}

register_feature() {
  local namespace="$1"
  local name="$2"
  local wait_seconds="$3"
  local elapsed=0
  local state

  state="$(az feature show --namespace "$namespace" --name "$name" --query properties.state --output tsv 2>/dev/null || true)"
  if [ "$state" != "Registered" ]; then
    echo "Registering Azure feature: $namespace/$name"
    az feature register --namespace "$namespace" --name "$name" --output none
  fi

  while true; do
    state="$(az feature show --namespace "$namespace" --name "$name" --query properties.state --output tsv 2>/dev/null || true)"
    if [ "$state" = "Registered" ]; then
      return
    fi
    if [ "$elapsed" -ge "$wait_seconds" ]; then
      fail "Timed out waiting for Azure feature $namespace/$name to register. Current state: ${state:-unknown}"
    fi
    echo "Waiting for Azure feature $namespace/$name to register. Current state: ${state:-unknown}"
    sleep 30
    elapsed=$((elapsed + 30))
  done
}

render_cache_model_selector() {
  if [ -n "$NIM_MODEL_PROFILE_ID" ]; then
    cat <<YAML
          model:
            profiles:
              - "$NIM_MODEL_PROFILE_ID"
YAML
  elif [ -n "$NIM_ENGINE" ]; then
    cat <<YAML
          model:
            engine: "$NIM_ENGINE"
            tensorParallelism: "$NIM_TENSOR_PARALLELISM"
YAML
  fi
}

render_service_cache_profile() {
  if [ -n "$NIM_MODEL_PROFILE_ID" ]; then
    cat <<YAML
      profile: "$NIM_MODEL_PROFILE_ID"
YAML
  fi
}

require_command az
require_command helm
require_command kubectl

az account show >/dev/null || fail "Azure CLI is not logged in. Run az login first."

require_true ALPAMAYO_MODEL_TERMS_ACCEPTED
require_true AKS_GPU_QUOTA_CONFIRMED

if [ "$GPU_MANAGEMENT_MODE" = "Managed" ] && [ "$INSTALL_GPU_OPERATOR" = "true" ]; then
  fail "GPU_MANAGEMENT_MODE=Managed conflicts with INSTALL_GPU_OPERATOR=true. Use the default AKS-managed GPU path or set GPU_MANAGEMENT_MODE=Unmanaged."
fi

case "$NIM_SERVER_DRY_RUN" in
  true|false) ;;
  *) fail "NIM_SERVER_DRY_RUN must be true or false." ;;
esac

echo "Using Alpamayo NIM image: $NIM_IMAGE_REPOSITORY:$NIM_IMAGE_TAG"

echo "Checking that $GPU_NODE_VM_SIZE is visible in $LOCATION"
SKU_COUNT="$(az vm list-skus \
  --location "$LOCATION" \
  --size "$GPU_NODE_VM_SIZE" \
  --query "length([?name=='$GPU_NODE_VM_SIZE'])" \
  --output tsv)"

[ "$SKU_COUNT" != "0" ] || fail "$GPU_NODE_VM_SIZE is not listed in $LOCATION for this subscription."

echo "Current quota for $GPU_QUOTA_FAMILY_NAME in $LOCATION; confirmed requirement: at least $GPU_REQUIRED_VCPUS available vCPUs."
az vm list-usage \
  --location "$LOCATION" \
  --query "[?name.value=='$GPU_QUOTA_FAMILY_NAME'].{Current:currentValue,Limit:limit}" \
  --output table

if [ "$GPU_MANAGEMENT_MODE" = "Managed" ]; then
  register_feature Microsoft.ContainerService ManagedGPUExperiencePreview "$MANAGED_GPU_FEATURE_WAIT_SECONDS"
fi

register_provider Microsoft.ContainerService
register_provider Microsoft.OperationalInsights
register_provider Microsoft.OperationsManagement
register_provider Microsoft.Insights

echo "Creating resource group: $RESOURCE_GROUP"
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

DEPLOYMENT_ARGS=(
  location="$LOCATION"
  namePrefix="$NAME_PREFIX"
  systemNodeVmSize="$SYSTEM_NODE_VM_SIZE"
  systemNodeCount="$SYSTEM_NODE_COUNT"
  gpuNodePoolName="$GPU_NODEPOOL_NAME"
  gpuNodeVmSize="$GPU_NODE_VM_SIZE"
  gpuNodeCount="$GPU_NODE_COUNT"
  gpuOsDiskSizeGB="$GPU_OS_DISK_SIZE_GB"
  gpuDriver="$GPU_DRIVER"
  gpuManagementMode="$GPU_MANAGEMENT_MODE"
  gpuMigStrategy="$GPU_MIG_STRATEGY"
  taintGpuNodePool="$TAINT_GPU_NODEPOOL"
  enableAzureMonitor="$ENABLE_AZURE_MONITOR"
)

if [ -n "$CLUSTER_NAME" ]; then
  DEPLOYMENT_ARGS+=(clusterName="$CLUSTER_NAME")
fi

echo "Deploying AKS infrastructure from Bicep"
DEPLOYED_CLUSTER_NAME="$(az deployment group create \
  --name "alpamayo-nim-$(date +%Y%m%d%H%M%S)" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters "${DEPLOYMENT_ARGS[@]}" \
  --query properties.outputs.clusterName.value \
  --output tsv)"

echo "Fetching kubeconfig for $DEPLOYED_CLUSTER_NAME"
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYED_CLUSTER_NAME" \
  --overwrite-existing \
  --output none

echo "Adding NVIDIA Helm repository"
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update >/dev/null
helm repo update nvidia >/dev/null

if [ "$INSTALL_GPU_OPERATOR" = "true" ]; then
  echo "Installing NVIDIA GPU Operator"
  helm upgrade --install gpu-operator nvidia/gpu-operator \
    --namespace gpu-operator \
    --create-namespace \
    --set-json 'daemonsets.tolerations=[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"},{"key":"sku","operator":"Equal","value":"gpu","effect":"NoSchedule"}]' \
    --set-json 'node-feature-discovery.worker.tolerations=[{"key":"node-role.kubernetes.io/control-plane","operator":"Equal","value":"","effect":"NoSchedule"},{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"},{"key":"sku","operator":"Equal","value":"gpu","effect":"NoSchedule"}]' \
    --wait \
    --timeout 45m
fi

echo "Installing NVIDIA NIM Operator"
helm upgrade --install nim-operator nvidia/k8s-nim-operator \
  --namespace nim-operator \
  --create-namespace \
  --version "$NIM_OPERATOR_VERSION" \
  --set-string 'operator.nodeSelector.kubernetes\.azure\.com/mode=system' \
  --wait \
  --timeout 15m

kubectl wait \
  --for=condition=Established \
  crd/nimcaches.apps.nvidia.com \
  crd/nimservices.apps.nvidia.com \
  --timeout=180s

echo "Creating NIM namespace"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

if [ "$NIM_SERVER_DRY_RUN" = "true" ]; then
  echo "Skipping NGC secret creation for server-side dry run"
else
  [ -n "${NGC_API_KEY:-}" ] || fail "Set NGC_API_KEY to an NVIDIA NGC API key."

  echo "Creating NGC secrets"
  kubectl create secret docker-registry ngc-secret \
    --namespace "$NAMESPACE" \
    --docker-server=nvcr.io \
    --docker-username='$oauthtoken' \
    --docker-password="$NGC_API_KEY" \
    --dry-run=client \
    -o yaml | kubectl apply -f -

  kubectl create secret generic ngc-api-secret \
    --namespace "$NAMESPACE" \
    --from-literal=NGC_API_KEY="$NGC_API_KEY" \
    --dry-run=client \
    -o yaml | kubectl apply -f -
fi

CACHE_MODEL_SELECTOR="$(render_cache_model_selector)"
NIM_CACHE_PROFILE="$(render_service_cache_profile)"
MANIFEST_FILE="$(mktemp "${TMPDIR:-/tmp}/alpamayo-nim.XXXXXX.yaml")"
trap 'rm -f "$MANIFEST_FILE"' EXIT

cat >"$MANIFEST_FILE" <<YAML
apiVersion: apps.nvidia.com/v1alpha1
kind: NIMCache
metadata:
  name: $NIM_CACHE_NAME
  namespace: $NAMESPACE
spec:
  source:
    ngc:
      modelPuller: "$NIM_IMAGE_REPOSITORY:$NIM_IMAGE_TAG"
      pullSecret: ngc-secret
      authSecret: ngc-api-secret
$CACHE_MODEL_SELECTOR
  storage:
    pvc:
      create: true
      storageClass: "$NIM_STORAGE_CLASS"
      size: "$NIM_CACHE_SIZE"
      volumeAccessMode: $NIM_VOLUME_ACCESS_MODE
  tolerations:
    - key: sku
      operator: Equal
      value: gpu
      effect: NoSchedule
  nodeSelector:
    workload: nim
  resources: {}
---
apiVersion: apps.nvidia.com/v1alpha1
kind: NIMService
metadata:
  name: $NIM_SERVICE_NAME
  namespace: $NAMESPACE
spec:
  image:
    repository: "$NIM_IMAGE_REPOSITORY"
    tag: "$NIM_IMAGE_TAG"
    pullPolicy: IfNotPresent
    pullSecrets:
      - ngc-secret
  authSecret: ngc-api-secret
  storage:
    nimCache:
      name: $NIM_CACHE_NAME
$NIM_CACHE_PROFILE
  replicas: 1
  resources:
    limits:
      nvidia.com/gpu: $NIM_GPU_LIMIT
  expose:
    service:
      type: $NIM_SERVICE_TYPE
      port: $NIM_SERVICE_PORT
  tolerations:
    - key: sku
      operator: Equal
      value: gpu
      effect: NoSchedule
  nodeSelector:
    workload: nim
YAML

if [ "$NIM_SERVER_DRY_RUN" = "true" ]; then
  echo "Validating Alpamayo NIM cache and service resources with server-side dry run"
  kubectl apply --dry-run=server -f "$MANIFEST_FILE"
  echo "Server-side dry run succeeded. Set NIM_SERVER_DRY_RUN=false and NGC_API_KEY to deploy the NIM resources."
  exit 0
fi

echo "Applying Alpamayo NIM cache and service resources"
kubectl apply -f "$MANIFEST_FILE"

echo "Deployment submitted. Watch progress with:"
echo "kubectl get nimcache,nimservice,pods,svc -n $NAMESPACE"

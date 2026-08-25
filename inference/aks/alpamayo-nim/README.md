# Deploy NVIDIA Alpamayo NIM on Azure Kubernetes Service

This sample provisions an Azure Kubernetes Service (AKS) cluster for an
NVIDIA Alpamayo NIM deployment. It includes:

- Bicep for an AKS cluster with a CPU system node pool and an AKS-managed GPU
  node pool
- Defaults for a one-node `Standard_NC24ads_A100_v4` GPU pool
- A deployment helper that installs the NVIDIA NIM Operator, NGC image pull
  secrets, a `NIMCache`, and a `NIMService`

The deployment is pinned to the Alpamayo 1.5 NIM container
`nvcr.io/nim/nvidia/alpamayo-1-5-10b:1.0.0`.

## Why This GPU SKU

Alpamayo 1.5 is a 10B VLA model with a documented minimum of one NVIDIA GPU
with at least 24 GB of VRAM. The default Azure SKU,
`Standard_NC24ads_A100_v4`, provides one NVIDIA A100 80 GB GPU, 24 vCPUs, and
220 GiB host memory, which is a practical initial deployment target.

Use `Standard_NC40ads_H100_v5` or a larger A100/H100 SKU if the selected
Alpamayo NIM profile requires Hopper, multiple GPUs, or more memory.

## Security and Cost Notice

GPU nodes are expensive and NIM model startup can download large artifacts. The
sample defaults to a single GPU node and a `ClusterIP` service. Do not change
`NIM_SERVICE_TYPE` to `LoadBalancer` for production without authentication,
network controls, and TLS.

Delete the resource group when you finish testing.

## Prerequisites

- Azure CLI logged in to your target subscription
- `kubectl`
- Helm 3
- Permission to create resource groups, AKS clusters, node pools, and managed
  identities used by AKS
- Quota for the chosen GPU VM family in the target Azure region
- An NVIDIA NGC API key with access to the selected Alpamayo NIM artifact
- Acceptance of the applicable Alpamayo model terms and NVIDIA NIM access terms

Useful references:

- [Alpamayo 1.5 model artifact](https://catalog.ngc.nvidia.com/orgs/nim/nvidia/models/alpamayo-1-5-10b/-)
- [Alpamayo 1.5 model card](https://huggingface.co/nvidia/Alpamayo-1.5-10B)
- [Alpamayo 1.5 NIM container](https://catalog.ngc.nvidia.com/orgs/nim/nvidia/containers/alpamayo-1-5-10b/-)
- [NVIDIA NIM on Azure AKS](https://docs.nvidia.com/nim/large-language-models/latest/deployment/csp-deployment/azure.html)
- [NVIDIA NIM Operator install guide](https://docs.nvidia.com/nim-operator/latest/install.html)
- [NVIDIA NIM Operator cache guide](https://docs.nvidia.com/nim-operator/latest/cache.html)
- [AKS GPU node pools](https://learn.microsoft.com/azure/aks/use-nvidia-gpu)
- [Azure NC A100 v4 VM sizes](https://learn.microsoft.com/azure/virtual-machines/sizes/gpu-accelerated/nca100v4-series)

## Configure

Run commands from this sample directory:

```bash
cd inference/aks/alpamayo-nim
az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"

export NGC_API_KEY="<YOUR_NGC_API_KEY>"
export ALPAMAYO_MODEL_TERMS_ACCEPTED="true"
export AKS_GPU_QUOTA_CONFIRMED="true"
```

Optional deployment settings:

```bash
export LOCATION="westeurope"
export RESOURCE_GROUP="rg-alpamayo-nim"
export NAME_PREFIX="alpamayo-nim"
export CLUSTER_NAME="aks-alpamayo-nim"

export GPU_NODE_VM_SIZE="Standard_NC24ads_A100_v4"
export GPU_NODE_COUNT="1"
export GPU_OS_DISK_SIZE_GB="512"
export GPU_DRIVER="Install"
export GPU_MANAGEMENT_MODE="Managed"
export TAINT_GPU_NODEPOOL="false"
export GPU_QUOTA_FAMILY_NAME="StandardNCADSA100v4Family"
export GPU_REQUIRED_VCPUS="24"

export NIM_IMAGE_REPOSITORY="nvcr.io/nim/nvidia/alpamayo-1-5-10b"
export NIM_IMAGE_TAG="1.0.0"
export NIM_SERVER_DRY_RUN="false"
```

The default Kubernetes service is internal:

```bash
export NIM_SERVICE_TYPE="ClusterIP"
```

For a temporary public endpoint during controlled testing:

```bash
export NIM_SERVICE_TYPE="LoadBalancer"
```

## Deploy

```bash
./deploy.sh
```

The script:

1. Checks Azure login and local tools.
2. Prints the selected GPU family quota for review.
3. Registers required Azure resource providers and the AKS managed GPU preview
   feature when needed.
4. Deploys `main.bicep`.
5. Fetches AKS credentials.
6. Installs the NVIDIA NIM Operator on the system node pool.
7. Creates NGC Kubernetes secrets.
8. Applies `NIMCache` and `NIMService` resources for the configured image.

To validate the live AKS/NIM Operator path and server-side CRD schema without
creating NGC secrets or starting Alpamayo, run:

```bash
export NIM_SERVER_DRY_RUN="true"
./deploy.sh
export NIM_SERVER_DRY_RUN="false"
```

## Monitor

```bash
kubectl get nodes -o wide
kubectl get pods -n nim-operator
kubectl get nimcache,nimservice,pods,svc -n "${NAMESPACE:-alpamayo-nim}"
```

If you use `NIM_SERVICE_TYPE=LoadBalancer`, retrieve the external address:

```bash
kubectl get svc -n "${NAMESPACE:-alpamayo-nim}" "${NIM_SERVICE_NAME:-alpamayo-nim}"
```

## Query Alpamayo

The default service is internal. For local testing, forward it to your machine:

```bash
kubectl port-forward -n "${NAMESPACE:-alpamayo-nim}" svc/"${NIM_SERVICE_NAME:-alpamayo-nim}" 8000:8000
```

In another terminal, verify the service and model:

```bash
curl -s http://localhost:8000/v1/health/ready
curl -s http://localhost:8000/v1/models | jq
```

For a simple visual question-answering smoke test, download a public driving
image and call `/v1/vqa` with base64 image data:

```bash
curl -L \
  -o your-driving-scene.jpg \
  https://raw.githubusercontent.com/udacity/CarND-LaneLines-P1/master/test_images/solidWhiteRight.jpg

base64 -i ./your-driving-scene.jpg | tr -d '\n' | jq -Rs '{
  model: "nvidia/alpamayo1.5",
  messages: [{
    role: "user",
    content: [
      {type: "text", text: "Describe this driving scene and identify safety-relevant observations."},
      {type: "image_url", image_url: {url: ("data:image/jpeg;base64," + .)}}
    ]
  }],
  max_tokens: 256
}' | curl -s http://localhost:8000/v1/vqa \
  -H "Content-Type: application/json" \
  --data-binary @- | jq
```

For trajectory inference, use `/v1/infer` or `/v1/chat/completions` with
Alpamayo-specific `nvext.egomotion`. The container's OpenAPI schema says
trajectory requests need `ego_history_xyz` with shape `[16, 3]` and
`ego_history_rot` with 16 quaternion rows `[w, x, y, z]` or 16 rotation
matrices.

## Validate Local Files

These checks do not create Azure resources:

```bash
az bicep build --file main.bicep --stdout >/dev/null
bash -n deploy.sh
```

## Clean Up

```bash
az group delete --name "${RESOURCE_GROUP:-rg-alpamayo-nim}" --yes --no-wait
```

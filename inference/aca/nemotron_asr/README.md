# Deploy NVIDIA Nemotron ASR Streaming NIM on Azure Container Apps

This sample deploys NVIDIA Nemotron ASR Streaming NIM to an Azure Container
Apps (ACA) serverless NVIDIA A100 GPU. It includes:

- Bicep templates for Azure Container Registry, Key Vault, managed identity,
  role assignments, the ACA environment, and the container app
- A deployment script with region, profile, quota, license, and ingress checks
- A Python client for testing the Riva streaming gRPC API
- Secure defaults: internal ingress, an immutable source image digest, managed
  identity for ACR pulls, and a Key Vault reference for the NGC API key

The NIM uses its multilingual profile and the client defaults to automatic
language detection.

## Security and Cost Notice

The service processes audio and consumes an A100 GPU. Do not expose it to the
public internet without access controls. External ingress in this sample
requires an explicit client IP CIDR allowlist.

The initial deployment keeps one replica warm because first startup can take
20 to 30 minutes. Set `MIN_REPLICAS=0` when the endpoint can be cold, or delete
the resource group after testing. With scale-to-zero, the first request can time
out while the model initializes.

Resources are tagged with `application`, `environment`, and `managedBy` so they
can be tracked in Azure Cost Management.

## Architecture

The deployment runs in three stages:

1. [`main.bicep`](main.bicep) creates ACR, Key Vault, managed identity, RBAC,
   and the ACA GPU environment.
2. [`deploy.sh`](deploy.sh) imports the digest-pinned NIM image from NGC into
   ACR.
3. [`app.bicep`](app.bicep) deploys the NIM after the private image is present.

The container app reads the NGC API key through a versionless Key Vault secret
reference. The key is not stored directly in the container app configuration.

## Prerequisites

- An Azure subscription where ACA serverless A100 GPUs are available
- Serverless A100 GPU quota in the deployment region
- Azure CLI and the Container Apps extension
- Bash 3.2 or later
- Permission to create the documented resources and role assignments
- An NVIDIA AI Enterprise license for self-hosting NVIDIA Speech NIM
- An NGC personal API key with NGC Catalog access
- Python 3.10 or later for the client test
- A local mono, uncompressed, 16-bit PCM WAV file

Creating the `AcrPull` and `Key Vault Secrets User` assignments requires
`Microsoft.Authorization/roleAssignments/write`. The Role Based Access Control
Administrator, User Access Administrator, and Owner roles include that action.

Review these current availability and licensing references before deployment:

- [ACA serverless GPU regions and quota](https://learn.microsoft.com/azure/container-apps/gpu-serverless-overview)
- [NVIDIA Speech NIM prerequisites](https://docs.nvidia.com/nim/speech/latest/get-started/prerequisites.html)
- [NGC access setup](https://docs.nvidia.com/nim/speech/latest/get-started/ngc-access-setup.html)
- [Nemotron ASR Streaming profiles](https://docs.nvidia.com/nim/speech/latest/asr/deploy-asr-models/nemotron-asr-streaming.html)

## Configure Azure CLI

```bash
az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
az extension add --name containerapp --upgrade --allow-preview true
```

The deployment script registers the required Azure resource providers. Provider
registration can require subscription-level permission.

## Configure Deployment

Run all commands in this sample directory:

```bash
cd inference/aca/nemotron_asr

export NGC_API_KEY="<YOUR_NGC_PERSONAL_API_KEY>"
export NVIDIA_AI_ENTERPRISE_LICENSE_ACCEPTED="true"
export ACA_GPU_QUOTA_CONFIRMED="true"
```

The license variable records your confirmation for the local preflight only. It
does not obtain or activate a license. The quota confirmation is required
because Azure's subscription usage response does not consistently include
serverless consumption-GPU quota.

Optional deployment settings:

```bash
export LOCATION="swedencentral"
export RESOURCE_GROUP="rg-nemotron-asr-aca"
export NAME_PREFIX="nemotron-asr"
export MIN_REPLICAS="1"
```

The source uses an immutable `linux/amd64` image digest to avoid changes from a
moving tag and multi-architecture import ambiguity:

```bash
export SOURCE_IMAGE="nvcr.io/nim/nvidia/nemotron-asr-streaming@sha256:044e60d13b12bf79efdb2aac0d88b3eaae766aba934a311b13962a2b4b511759"
export TARGET_IMAGE="nvidia/nemotron-asr-streaming:amd64"
```

Before updating the digest, verify the image against the NVIDIA ASR NIM support
matrix and test startup and transcription again. Do not use `latest` in a
repeatable deployment.

## Choose Ingress

Internal ingress is the default:

```bash
export EXTERNAL_INGRESS="false"
```

An internal endpoint is reachable only from the ACA environment or connected
private network. To test from one known public client, explicitly enable
external ingress and provide its public IP as a `/32` CIDR:

```bash
export EXTERNAL_INGRESS="true"
export ALLOWED_IP_CIDR="<PUBLIC_EGRESS_IPV4>/32"
```

Use the public egress IPv4 address of the machine or network that will run the
Python client. Run the following from that client network if you need to look it
up:

```bash
curl -4 --fail --silent --show-error https://api.ipify.org
```

When deploying from Azure Cloud Shell but testing from your workstation, use
your workstation or corporate VPN egress address, not the Cloud Shell address.
The address can change when a VPN reconnects or an ISP assigns a new address; in
that case, update `ALLOWED_IP_CIDR` and deploy again.

Do not use `0.0.0.0/0`. For production or multiple client networks, use private
networking, mTLS, or a purpose-built authenticated API layer in front of NIM.

## Deploy

```bash
./deploy.sh
```

Before creating resources, the script:

- verifies Azure login and required local variables
- checks that the selected GPU workload profile is available in the region
- prints the current Container Apps quota response for review
- refuses external ingress without an IP allowlist

The deployment normally takes several minutes. NIM initialization can take an
additional 20 to 30 minutes.

ACR artifact streaming is optional and currently an Azure preview feature. To
enable it explicitly:

```bash
export ENABLE_ARTIFACT_STREAMING="true"
./deploy.sh
```

## Monitor Startup

Get the latest revision and follow its console logs:

```bash
export CONTAINER_APP_NAME="nemotron-asr-nim"

export LATEST_REVISION="$(
  az containerapp show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.latestRevisionName \
    --output tsv
)"

az containerapp logs show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --revision "$LATEST_REVISION" \
  --tail 100 \
  --follow
```

The NIM is ready when the logs report that the Riva gRPC server is listening on
port `50051` and ready.

For external ingress, retrieve the endpoint:

```bash
export ASR_FQDN="$(
  az containerapp show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn \
    --output tsv
)"

echo "${ASR_FQDN}:443"
```

## Test Streaming ASR

Create an isolated Python environment:

```bash
python3 -m venv .venv-asr
source .venv-asr/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Convert audio when necessary:

```bash
ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav
```

From a client allowed by the ingress configuration, run multilingual automatic
language detection:

```bash
python examples/test_nemotron_asr_streaming.py \
  --server "${ASR_FQDN}:443" \
  --use-ssl \
  --input-file /path/to/output.wav \
  --language-code auto
```

To constrain recognition to one supported language, replace `auto` with a code
such as `en-US` or `fr-FR`.

## Validate Local Files

The checks below do not create Azure resources:

```bash
az bicep build --file main.bicep --stdout >/dev/null
az bicep build --file app.bicep --stdout >/dev/null
bash -n deploy.sh
python -m unittest discover -s tests -v
```

## Operational Notes

- ACA terminates public TLS on port `443` and forwards HTTP/2 to NIM gRPC port
  `50051`.
- NIM HTTP port `9000` is not exposed by this container app.
- The startup probe allows approximately 40 minutes for initial model setup.
- `WorkLoad Profile Full` indicates regional GPU capacity is unavailable. Retry
  later or use another supported region.
- Role assignments can take several minutes to propagate. If image pull or Key
  Vault access initially fails, wait and restart the revision.
- Setting `MIN_REPLICAS=0` reduces idle GPU cost but introduces a long cold
  start. Set it back to `1` before latency-sensitive use.
- Add Azure Monitor alerts, budgets, diagnostic settings, private networking,
  and centralized audit retention before production use.

## Cleanup

The sample creates a dedicated resource group. Verify the name before deleting
it because this removes every resource in that group:

```bash
az group show --name "$RESOURCE_GROUP" --output table
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

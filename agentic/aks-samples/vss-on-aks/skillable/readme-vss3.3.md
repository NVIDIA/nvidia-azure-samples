###Please login to the VM using the following credentials:

- Username: +++@lab.VirtualMachine(ignite26NIMS11).Username+++

- Password: +++@lab.VirtualMachine(ignite26NIMS11).Password+++

- 
###Accessing the Azure Portal

1. [] Please open the Edge Browser from the VM desktop

1. [] Please naviage to +++**https://portal.azure.com**+++

1. [] Login to Azure using the following credentials:

- Username: +++**@lab.CloudPortalCredential(User1).Username**+++

- When asked for a "Temporary Access Pass", click on the Resources tab, and copy the value from "TAP"


!IMAGE[Skillable_TAP.png](instructions359732/Skillable_TAP.png)
<!--Hidden
- Password: +++**@lab.CloudPortalCredential(User1).Password**+++ -->





# Overview

This workshop guides you through deploying the NVIDIA [Video Search and Summarization (VSS) Long Video Summarization (LVS) blueprint](https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard)  on an existing Azure Kubernetes Service (AKS) cluster with **one available compatible GPU**. The lab cluster is `rag-demo`, with the GPU node pool `gpupool`. The node pool exists; GPU Operator, driver, container toolkit, and device-plugin readiness are established during the lab.

The NVIDIA AI Blueprint for Video Search and Summarization (VSS) makes it easy to start building and customizing video analytics AI agents. These insightful, accurate, and interactive agents are powered by generative AI, vision language models (VLMs), large language models (LLMs), and NVIDIA NIM™ Microservices-helping a variety of industries make better decisions, faster. They can be given tasks through natural language and perform complex operations like video summarization and visual question-answering, unlocking entirely new application possibilities.




# Key Features
* Real-time and batch processing modes
* Video search
* Video summarization
* Interactive Question and Answering (Q&A)
* Alerts
* Event reviewer and verification
* Object tracking
* Multimodal model fusion

# Key Benefits
* Build video analytics AI agents that can analyze, interpret, and process vast amounts of video data at scale.
* Produce summaries of long videos up to 100X faster than going through the videos manually.
* Accelerate development time by bringing together various generative AI models and services to quickly build AI agents.
* Augment traditional computer vision pipelines with VLMs to provide deep video understanding.
* Provide a range of optimized deployments, from the enterprise edge to cloud.

The VSS 3.3 LVS workflow is a reference agent that generates structured video analysis reports powered by
Cosmos 3 Nano Reasoner in RT-VLM and a text-generation LLM accessed through Azure API Management (APIM). Long videos are routed to the LVS microservice, which
chunks, analyzes and synthesizes results through a Context-Aware RAG pipeline. The results are then passed to the report agent for PDF output.

!IMAGE[abgzi6rv.png](instructions359732/abgzi6rv.png)

Services are exposed through one Azure public IP using the **HAProxy Kubernetes
Ingress** controller and [`nip.io`](https://nip.io/) hostnames. This lab targets
VSS `v3.3.0rc0` and an existing Ubuntu AKS GPU pool, such as two
`Standard_NC24ads_A100_v4` nodes (one A100 80 GB per node). The VSS release
reserves only one GPU. Follow the tasks in order from a fresh workshop
installation; no earlier VSS or NVIDIA Helm release is assumed.

The chart configuration is checked locally by rendering Helm manifests. Complete
the upload, video analysis, and report exercises to validate the full deployment;
a running UI or successful text-model call alone does not validate summarization.

Text-to-text requests from the VSS Agent (planning, Q&A, and report writing),
the LVS Context-Aware RAG summarization/merging pipeline, and the agent's optional
LLM evaluator use the shared APIM gateway. Video/frame understanding continues
through the in-cluster RT-VLM/Cosmos service. Embeddings are disabled in this LVS
profile; the gateway does not expose an embeddings or video inference API.

The single-GPU allocation is:

| Component | Execution |
| --- | --- |
| Text generation (agent, reports, LVS merging, optional evaluator) | Existing APIM gateway; no cluster GPU |
| RT-VLM / Cosmos video understanding | 1 GPU |
| LVS summarization orchestration | CPU; delegates video inference to RT-VLM and text generation to APIM |
| VIOS stream processing | CPU software encode/decode; no GPU |

Use a short video and one analysis at a time. CPU video processing can be slower
than the default hardware path. One GPU must be available to this release, and
its model/VRAM must support the integrated Cosmos checkpoint; a GPU count
alone does not establish model compatibility.

# Prerequisites

- The existing `rag-demo` AKS cluster and `gpupool` node pool in the lab subscription, with at least one available compatible GPU. The pool can contain two GPUs; this release reserves one in steady state. The lab installs GPU support as needed but does not create or resize the cluster.

- A lab-provided **gateway bearer key** for the existing [ignite-model-gateway APIM service](https://portal.azure.com/#@NVIDIA.onmicrosoft.com/resource/subscriptions/7d5234cf-1437-4626-94d3-47372cdfc8f5/resourceGroups/rg-halkemade-model-gateway/providers/Microsoft.ApiManagement/service/ignite-model-gateway/overview). Obtain it from the instructor; this is separate from your NVIDIA API key and an APIM subscription key.
- Permission to install cluster-scoped GPU Operator resources and to inspect the existing GPU nodes.
- Outbound HTTPS access to the APIM gateway, NGC model/registry endpoints, GitHub/GHCR, Docker Hub, and the Helm repositories used below.

- An NVIDIA/NGC API key authorized to download the checkpoint `nim/nvidia/cosmos3-nano-reasoner:bf16-final` and required NGC artifacts. Complete any required model access/terms in the NVIDIA catalog before the lab. An inference API key is sufficient only if it has the required download permissions; its prefix alone does not prove access.
- A short, non-sensitive H.264 MP4 for the exercise; start with a 10-30 second clip.

------
# Task 1: Environment Configuration

### 1. Set up Cloud Shell

1. [Open Cloud Shell](https://portal.azure.com/)

Once you log in, click on the Cloud Shell button, located at the top bar:
!IMAGE[Azure_Cloud_Shell.png](instructions359732/Azure_Cloud_Shell.png)


(Note: if it's not visible, either make the Lab window larger or click on the 3 dots):
!IMAGE[Azure_Cloud_Shell_Expand.png](instructions359732/Azure_Cloud_Shell_Expand.png)

2. When asked, select "Bash"
!IMAGE[Bash.png](instructions359732/Bash.png)

3. When asked, select "No Storage", the only available Subscription, and click "Apply"
!IMAGE[Subscription.png](instructions359732/Subscription.png)


### 2. Configure NVIDIA API Key

The first step in the pipeline is to deploy an inference NIM - this will be used to host the LLM that will be responsible for generating responses for a given set of user queries. In order to get started with NIM, we'll need to make sure we have access to an [NVIDIA API key](https://build.nvidia.com/settings/api-keys). We can export this key to be used an environment variable:

The text-generation LLM is remote, but RT-VLM downloads Cosmos from NGC.
Enter your authorized NVIDIA/NGC key without printing it:

```bash
read -r -s -p "NVIDIA/NGC key with Cosmos download access: " NGC_API_KEY
printf '\n'
export NGC_API_KEY
: "${NGC_API_KEY:?Enter your NVIDIA/NGC API key}"
```



### 3. Select the existing cluster and set up environment variables


```
export RESOURCE_GROUP=@lab.CloudResourceGroup(ResourceGroup1).Name
export CLUSTER_NAME="rag-demo"
export NAMESPACE="vss"
export RELEASE="vss-lvs"
export WORKSHOP_DIR="$HOME/vss-workshop"
mkdir -p "$WORKSHOP_DIR/aks/ingress" "$WORKSHOP_DIR/aks/values"
```


### 4. Connect to the existing AKS cluster

```bash
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$CLUSTER_NAME"
kubectl config current-context
kubectl get nodes -o wide
az aks nodepool show --resource-group "$RESOURCE_GROUP" \
  --cluster-name "$CLUSTER_NAME" --name gpupool \
  --query '{Pool:name,VMSize:vmSize,Nodes:count,Power:powerState.code,Provisioning:provisioningState}' -o table
```

Confirm the context is `rag-demo`, the existing GPU pool is running, and its VM
SKU is GPU-capable. A pool named `gpupool` or marked Running in the Azure Portal
does not by itself establish GPU driver or device-plugin readiness.

------

# Task 2: Install GPU support on the existing node pool

### 1. Inspect the node image before selecting driver ownership

GPU Operator is the lab's manager for the NVIDIA device plugin and discovery.
It can also install the driver and container toolkit. AKS GPU images can already
supply those host components, so inspect them rather than assuming they exist
or installing a second driver manager.

```bash
kubectl get nodes -l agentpool=gpupool \
  -o 'custom-columns=NODE:.metadata.name,OS:.status.nodeInfo.osImage,KERNEL:.status.nodeInfo.kernelVersion'

for node in $(kubectl get nodes -l agentpool=gpupool -o name); do
  kubectl debug "$node" -it --image=ubuntu:24.04 --profile=sysadmin -- \
    chroot /host /bin/sh -c '
      echo "GPU driver check:"
      if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
      else
        echo "NVIDIA driver tools not installed"
      fi
      echo "Container toolkit check:"
      if command -v nvidia-container-runtime >/dev/null 2>&1; then
        nvidia-container-runtime --version
      else
        echo "NVIDIA container runtime not installed"
      fi
    '
done
```

These node-debug sessions inspect the host; they do not request a GPU allocation.
Use the results on **all GPU nodes** to select the initial installation settings:

| Host result | `GPU_INSTALL_DRIVER` | `GPU_INSTALL_TOOLKIT` |
| --- | --- | --- |
| Driver and toolkit absent | `true` | `true` |
| Working `nvidia-smi`; toolkit absent | `false` | `true` |
| Working `nvidia-smi` and toolkit already supplied by AKS | `false` | `false` |

For the first row, use:

```bash
export GPU_INSTALL_DRIVER=true
export GPU_INSTALL_TOOLKIT=true
```

For another row, set those two variables to that row's values before continuing.
A driver must load successfully, not merely have an `nvidia-smi` binary present.
If node images have mixed or failing host configurations, have the instructor
establish a consistent supported Ubuntu GPU pool before installation. RT-VLM
lists NVIDIA driver 580 or newer in its software requirements; check that the
selected AKS image or Operator driver meets the image's requirements.

### 2. Install NVIDIA GPU Operator

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
helm repo update nvidia

helm install gpu-operator nvidia/gpu-operator \
  --version v26.7.1 \
  --namespace gpu-operator --create-namespace \
  --set driver.enabled="$GPU_INSTALL_DRIVER" \
  --set toolkit.enabled="$GPU_INSTALL_TOOLKIT" \
  --set devicePlugin.enabled=true \
  --set operator.runtimeClass=nvidia-container-runtime \
  --wait --timeout 20m
```

The Operator installs the missing host components selected above and deploys
its device plugin. Use this single Operator-managed plugin; a separate
`nvdp/nvidia-device-plugin` installation is not part of this fresh setup.
For AKS-provided driver/toolkit components, NVIDIA's AKS installation guidance
uses `driver.enabled=false` and `toolkit.enabled=false`.

### 3. Verify GPU readiness before installing VSS

```bash
kubectl get pods -n gpu-operator -o wide
kubectl get daemonsets -n gpu-operator
kubectl get nodes -l agentpool=gpupool \
  -o 'custom-columns=NAME:.metadata.name,GPU-CAPACITY:.status.capacity.nvidia\.com/gpu,GPU-ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'
kubectl describe nodes -l agentpool=gpupool
```

Continue only when the Operator's driver/toolkit components, where enabled,
and device-plugin pods are ready, validator jobs complete successfully, and
Kubernetes advertises the GPU resources. Each `Standard_NC24ads_A100_v4` node
should advertise `nvidia.com/gpu: 1`; a two-node pool supplies two GPUs total.
At least one GPU must be unreserved for VSS. `Allocatable` is total schedulable
capacity, not the currently free count; review **Allocated resources** and
**Non-terminated Pods** in the node descriptions.

------

# Task 3: Configure and test the APIM text-model endpoint

### 1. Set the gateway URL and model alias

The Azure Portal link opens the management page. Model requests use the gateway
URL below. The existing `aiq-models` API exposes `GET /models` and
`POST /chat/completions` under `/openai/v1` and authenticates with
`Authorization: Bearer <gateway key>`.


```bash
export APIM_LLM_BASE_URL=@lab.Variable(APIM_LLM_BASE_URL)
export APIM_LLM_MODEL="Nemotron3Ultra"
export APIM_API_KEY=@lab.Variable(APIM_API_KEY)

```

Use `NemotronLightning` for the lab. `Nemotron3Ultra` is the other advertised
alias. Provider model IDs such as `nvidia/nemotron-3.5-lightning-30b-a3b` are not
accepted by this gateway. Keep the shared base URL at `/openai`, without a
trailing slash or `/v1`: the VSS Agent adds `/v1`, and LVS adds it when absent.

### 2. Verify authentication and chat completion

```bash
curl --fail-with-body --silent --show-error --connect-timeout 10 --max-time 30 \
  "${APIM_LLM_BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${APIM_API_KEY}"

curl --fail-with-body --silent --show-error --connect-timeout 10 --max-time 180 \
  "${APIM_LLM_BASE_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${APIM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with a short greeting."}],"max_tokens":1024,"reasoning_effort":"none","stream":false}' "$APIM_LLM_MODEL")"
```

Continue only when `/models` lists the selected alias and the completion returns
HTTP 200 with a non-empty `choices[0].message.content`. A successful model list
alone does not validate inference. Do not enable shell tracing (`set -x`) or
paste keys into values files, screenshots, or source control.

This profile disables local text-model NIMs, so the NIM Operator and the A100
Nemotron cache patch are not required. The GPU support installed in Task 2 is for
RT-VLM; summarization orchestration and VIOS run on CPU in this lab.

------
# Task 4: Install HAProxy ingress

The VSS LVS profile expects `global.externalHost` at chart install time. The UI receives browser-reachable API addresses through its `NEXT_PUBLIC_*` settings. We therefore install the ingress controller first, capture the Azure-provisioned LoadBalancer IP, then install VSS using that IP.

### 1. Download the updated workshop files

```bash
export WORKSHOP_DIR="$HOME/vss-workshop"
mkdir -p "$WORKSHOP_DIR"
cd "$WORKSHOP_DIR"
export WORKSHOP_SOURCE="https://raw.githubusercontent.com/NVIDIA/nvidia-azure-samples/fbb61a6d96d4f1b14d6b66ba00d24a100d924f54/agentic/aks-samples/vss-on-aks"
mkdir -p aks/ingress aks/values
curl -fsSL "$WORKSHOP_SOURCE/aks/ingress/haproxy-values.yaml" -o aks/ingress/haproxy-values.yaml
curl -fsSL "$WORKSHOP_SOURCE/aks/values/values-aks-a100.yaml" -o aks/values/values-aks-a100.yaml
```

### 2. Add the HAProxy Helm repository

```bash
helm repo add haproxytech https://haproxytech.github.io/helm-charts --force-update && helm repo update haproxytech
```

### 3. Install HAProxy ingress

```bash
helm install vss-haproxy haproxytech/kubernetes-ingress --version 1.54.2 -n vss-ingress --create-namespace -f "$WORKSHOP_DIR/aks/ingress/haproxy-values.yaml" --wait --timeout 5m

kubectl get ingressclass haproxy
```
### 4. Discover the public IP through the Kubernetes API

Wait for Azure to publish the ingress LoadBalancer's public IP in its Service
status, then read it directly from the Kubernetes API. No Portal lookup or
manual IP substitution is required:

```bash
kubectl wait -n vss-ingress service/vss-haproxy-kubernetes-ingress \
  --for=jsonpath='{.status.loadBalancer.ingress[0].ip}' --timeout=10m

export EXTERNAL_HOST=$(kubectl -n vss-ingress \
  get service vss-haproxy-kubernetes-ingress \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

python3 - <<'PYCODE'
import ipaddress
import os

address = ipaddress.ip_address(os.environ.get("EXTERNAL_HOST", ""))
if address.version != 4:
    raise SystemExit("This lab's nip.io hostname requires an ingress IPv4 address")
print(f"VSS URL: http://vss.{address}.nip.io/")
PYCODE
```

Continue only when the wait and IP validation succeed. This is the ingress
Service's public IP, not a GPU node's private IP or the APIM gateway address.

------

## Task 5: Deploy VSS LVS Blueprint

### 1. Prepare the pinned release for APIM and chat-completions requests

Run from the workshop directory configured in Task 1.

```bash
cd "$WORKSHOP_DIR"
git clone --branch v3.3.0rc0 --single-branch \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git
cd video-search-and-summarization

# This RC's LVS chart emits plain env values only. Replace its NVIDIA_API_KEY
# slot with a Secret reference; the agent chart already supports valueFrom.
python3 - <<'PYCODE'
from pathlib import Path

path = Path("deploy/helm/services/video-summarization/templates/deployment.yaml")
original = '              value: {{ index $envMap $k | quote }}'
replacement = """              {{- if eq $k "NVIDIA_API_KEY" }}
              valueFrom:
                secretKeyRef:
                  name: vss-apim-api
                  key: api-key
              {{- else }}
              value: {{ index $envMap $k | quote }}
              {{- end }}"""
text = path.read_text()
if replacement not in text:
    if text.count(original) != 1:
        raise SystemExit("Unexpected LVS template; stop and check the source tag")
    path.write_text(text.replace(original, replacement))

# The optional text evaluator must also use the OpenAI-compatible client.
config = Path("deploy/helm/developer-profiles/dev-profile-lvs/configs/vss-agent/config.yml")
original = "  eval_llm_judge:\n    _type: nim\n"
replacement = "  eval_llm_judge:\n    _type: openai\n"
text = config.read_text()
if replacement not in text:
    if text.count(original) != 1:
        raise SystemExit("Unexpected evaluator config; stop and check the source tag")
    config.write_text(text.replace(original, replacement))

text = config.read_text()
text = text.replace("  llm_reasoning: true\n", "  llm_reasoning: false\n")
config.write_text(text)

# Preserve the full profile UI settings, changing only the reasoning defaults.
import re
values = Path("deploy/helm/developer-profiles/dev-profile-lvs/values.yaml")
text = values.read_text()
pattern = r'("name":"llm_reasoning"[^}]*"default-value":)true'
text, count = re.subn(pattern, r'\1false', text)
if count == 0 and '"name":"llm_reasoning"' not in text:
    raise SystemExit("Unexpected UI parameter schema; check the source tag")
values.write_text(text)
PYCODE

helm dependency build deploy/helm/developer-profiles/dev-profile-lvs --skip-refresh
```

The release moved the LVS chart to `deploy/helm/developer-profiles/dev-profile-lvs`.
The setup gives summarization a Secret-backed `NVIDIA_API_KEY`, selects the
OpenAI client for the optional evaluator, and defaults text-client reasoning to
off in the agent and both UI panels. LVS uses `NVIDIA_API_KEY` as its LLM bearer
key. NGC pull secrets and RT-VLM checkpoint credentials use your NVIDIA key. Apply this adjustment before building dependencies so Helm
packages the modified service chart.

The source tag pins the charts. Some RC images use `develop-latest`, so their
contents can change between deployments even with the source tag pinned.

### 2. Create NGC and APIM secrets outside Helm values

```bash
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml \
  | kubectl apply -f -

kubectl create secret generic ngc-api -n "$NAMESPACE" \
  --from-literal=NGC_API_KEY="$NGC_API_KEY" \
  --from-literal=NGC_CLI_API_KEY="$NGC_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret docker-registry ngc-secret -n "$NAMESPACE" \
  --docker-server=nvcr.io \
  --docker-username='$oauthtoken' \
  --docker-password="$NGC_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

: "${APIM_API_KEY:?Set the gateway bearer key from Task 3}"
kubectl create secret generic vss-apim-api -n "$NAMESPACE" \
  --from-literal=api-key="$APIM_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -
unset APIM_API_KEY
```
The A100 values set `ngc.createSecrets: false`. These Secrets supply credentials
without storing key values in Helm release settings. RT-VLM uses `ngc-api`;
text-generation clients use `vss-apim-api`.

### 3. Create the APIM Helm overlay

```bash
cat > "$WORKSHOP_DIR/aks/values/values-apim.yaml" <<'YAML'
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

global:
  llmBaseUrl: https://ignite-model-gateway.azure-api.net/openai
  llmName: NemotronLightning

# Disable the local text LLM; RT-VLM's integrated Cosmos checkpoint stays local.
nims:
  enabled: false

agent:
  vss-agent:
    extraEnv:
      - name: LLM_MODEL_TYPE
        value: openai
      - name: OPENAI_API_KEY
        valueFrom:
          secretKeyRef:
            name: vss-apim-api
            key: api-key
      # Keep the declared compatibility nim text profile on the same credentials.
      - name: NVIDIA_API_KEY
        valueFrom:
          secretKeyRef:
            name: vss-apim-api
            key: api-key
YAML
```

The agent's `openai` client supports the APIM alias without NVIDIA model-catalog
lookup. The patched evaluator also uses the OpenAI client, and the shared settings
supply `EVAL_LLM_JUDGE_NAME` and `EVAL_LLM_JUDGE_BASE_URL`. LVS inherits the same
model and URL, retaining the profile's `RTVI_VLM_URL`, passthrough, and LLM merging settings. If the instructor
selects `Nemotron3Ultra`, update both `APIM_LLM_MODEL` and `global.llmName` here.

### 4. Configure a single GPU-consuming pod

The upstream Helm defaults reserve GPUs for RT-VLM, summarization, and VIOS.
For this lab, LVS delegates video inference to RT-VLM and text generation to
APIM, so remove its local GPU reservation. Keep VIOS on its supported software
encode/decode path. Configure exactly one RT-VLM replica with one GPU:

```bash
cat > "$WORKSHOP_DIR/aks/values/values-1gpu.yaml" <<'YAML'
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The single GPU-consuming pod: integrated Cosmos video inference.
rtvi:
  vss-rtvi-vlm:
    replicas: 1
    resources:
      requests:
        nvidia.com/gpu: 1
      limits:
        nvidia.com/gpu: 1

# Orchestration only: video inference goes to RT-VLM, text inference to APIM.
vss-summarization:
  resources: null

vios:
  vss-vios-streamprocessing:
    useSoftwarePath: true
    resources: null
YAML
```

Keep the RT-VLM GPU request and limit. No GPU sharing is required. `resources:
null` removes the inherited GPU reservations for summarization and VIOS; it does
not move Cosmos inference onto CPU. The other blueprint services remain separate
CPU pods. Keep the profile's RT-VLM passthrough and disabled embeddings settings.

This overlay has been checked by rendering the pinned Helm charts. Validate
startup and a short video's upload, summarization, and report output before
using it for the workshop: the moving `develop-latest` images can differ from
the checked-in source.

### 5. Configure upload addresses for the browser UI

The UI needs a public VIOS/VST API address, while its server-side proxy uses
cluster DNS. Generate the overlay before the initial chart installation. This
step queries the Kubernetes API again, so it does not depend on manually
copying an IP or retaining an earlier shell variable:

```bash
python3 - <<'PYCODE'
import ipaddress
import json
import os
from pathlib import Path
import subprocess

service = json.loads(subprocess.check_output([
    "kubectl", "get", "service", "vss-haproxy-kubernetes-ingress",
    "-n", "vss-ingress", "-o", "json",
], text=True))
entries = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
if not entries or not entries[0].get("ip"):
    raise SystemExit("Ingress public IP is not assigned; complete Task 4 first")
address = ipaddress.ip_address(entries[0]["ip"])
if address.version != 4:
    raise SystemExit("This lab requires an ingress IPv4 address")
host = f"vss.{address}.nip.io"
api_url = f"http://{host}/vst/api"
values = Path(os.environ["WORKSHOP_DIR"]) / "aks/values/values-ui.yaml"
values.parent.mkdir(parents=True, exist_ok=True)
values.write_text(f"""# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

vss-agent-ui:
  vstApiUrl: "{api_url}"
  proxyBaseUrl: "http://vss-vios-ingress:30888"
  extraEnv:
    - name: NEXT_PUBLIC_VIOS_API_URL
      value: "{api_url}"
""")
print(f"Created {values}")
print(f"Browser upload API: {api_url}")
PYCODE

export EXTERNAL_HOST=$(kubectl -n vss-ingress \
  get service vss-haproxy-kubernetes-ingress \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
: "${EXTERNAL_HOST:?Ingress public IP must be assigned before installing VSS}"
cat "$WORKSHOP_DIR/aks/values/values-ui.yaml"
```

The chart's `vstApiUrl` supplies `NEXT_PUBLIC_VST_API_URL`; the explicit VIOS
alias supports the UI image's VIOS naming. Both browser-facing names point to
the same public `/vst/api` path. `VSS_PROXY_BASE_URL` remains internal. A
server-side proxy address alone does not configure browser uploads.

The checkout configuration step also defaults **LLM Reasoning** to **off** in
the agent and both UI chat panels. Keep it off for this chat-completions-only
APIM gateway: the OpenAI client's Responses-style reasoning options can select
`/responses`, which this gateway does not expose. Planning, tool calls, video
analysis, and summarization remain available; this setting does not establish
whether the backend model performs internal thinking.

### 6. Install the LVS chart

```bash
cd deploy/helm/developer-profiles

helm install "$RELEASE" ./dev-profile-lvs \
  -f ./dev-profile-lvs/values-lvs.yaml \
  -f "$WORKSHOP_DIR/aks/values/values-aks-a100.yaml" \
  -f "$WORKSHOP_DIR/aks/values/values-apim.yaml" \
  -f "$WORKSHOP_DIR/aks/values/values-1gpu.yaml" \
  -f "$WORKSHOP_DIR/aks/values/values-ui.yaml" \
  --set global.externalHost="vss.${EXTERNAL_HOST}.nip.io" \
  --set global.kibanaPublicUrl="http://kibana.vss.${EXTERNAL_HOST}.nip.io" \
  -n "$NAMESPACE" --timeout 10m
```
`managed-csi-premium` is the configured Azure StorageClass. Verify it is
available with `kubectl get sc` before installation.

### 7. Wait for workloads and verify the installed configuration

```bash
kubectl get pvc -n "$NAMESPACE"
kubectl get pods -n "$NAMESPACE"
```

Wait for all PVCs to be `Bound` and long-running pods to be `Running` and Ready.
First Cosmos checkpoint download and initialization can take several minutes.
Only RT-VLM reserves a GPU. Summarization orchestration runs on CPU, and VIOS
stream processing uses CPU software encode/decode. This install does not create a Nemotron `NIMCache`
or `NIMService`.

```bash
kubectl get pods -n "$NAMESPACE" \
  -o 'custom-columns=POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName,GPU-REQUESTS:.spec.containers[*].resources.requests.nvidia\.com/gpu'
kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp | tail -30
```

Expect exactly one GPU-consuming pod, `vss-rtvi-vlm-...`, requesting `1` GPU.
Summarization and VIOS stream processing have no GPU request or limit.
`Completed` initialization jobs are normal. First image pulls and checkpoint
downloads can take several minutes. Summarization's `wait-vlm-endpoint` init
container waits for RT-VLM to become reachable; readiness is required before
starting the UI exercises.

Inspect the two Deployments without exposing the key:

```bash
kubectl get deployment vss-agent vss-summarization -n "$NAMESPACE" -o json \
  | python3 -c '
import json, sys
names = {"LLM_MODE", "LLM_MODEL_TYPE", "LLM_BASE_URL", "LLM_NAME",
         "EVAL_LLM_JUDGE_NAME", "EVAL_LLM_JUDGE_BASE_URL",
         "LVS_LLM_BASE_URL", "LVS_LLM_MODEL_NAME",
         "OPENAI_API_KEY", "NVIDIA_API_KEY", "RTVI_VLM_URL"}
for deployment in json.load(sys.stdin)["items"]:
    print(deployment["metadata"]["name"])
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        for entry in container.get("env", []):
            if entry["name"] in names:
                if entry["name"].endswith("API_KEY"):
                    value = entry.get("valueFrom", {}).get("secretKeyRef", {})
                    print(" ", entry["name"], "secretKeyRef:", value)
                else:
                    print(" ", entry["name"], entry.get("value", ""))
'
```

Expect the agent's base URL to end in `/openai`, LVS's to end in `/openai/v1`,
and both text-model names to be `NemotronLightning`. The agent must show
`LLM_MODE=remote`, `LLM_MODEL_TYPE=openai`; all displayed API-key entries must
reference `vss-apim-api` / `api-key`. `RTVI_VLM_URL` must remain the in-cluster
RT-VLM service. Do not continue if a text-model URL still targets the local
Nemotron service.

Verify the UI's public addresses and RT-VLM's model/key reference without
printing any secret values:

```bash
kubectl get deployment vss-agent-ui -n "$NAMESPACE" -o json \
  | python3 -c '
import ipaddress, json, subprocess, sys
service = json.loads(subprocess.check_output([
    "kubectl", "get", "service", "vss-haproxy-kubernetes-ingress",
    "-n", "vss-ingress", "-o", "json"], text=True))
address = ipaddress.ip_address(service["status"]["loadBalancer"]["ingress"][0]["ip"])
url = f"http://vss.{address}.nip.io/vst/api"
expected = {
    "NEXT_PUBLIC_VST_API_URL": url,
    "NEXT_PUBLIC_VIOS_API_URL": url,
    "VSS_PROXY_BASE_URL": "http://vss-vios-ingress:30888",
}
deployment = json.load(sys.stdin)
container = next(c for c in deployment["spec"]["template"]["spec"]["containers"]
                 if c["name"] == "vss-agent-ui")
entries = container.get("env", [])
failures = []
for name, wanted in expected.items():
    matches = [e for e in entries if e["name"] == name]
    actual = matches[0].get("value") if len(matches) == 1 else None
    print(name + "=" + (actual if actual is not None else "<missing or duplicated>"))
    if actual != wanted:
        failures.append(name)
if failures:
    raise SystemExit("UI configuration validation failed: " + ", ".join(failures))
print("UI public API addresses and internal proxy verified")
'

kubectl get deployment vss-rtvi-vlm -n "$NAMESPACE" \
  -o jsonpath='{range .spec.template.spec.containers[*].env[*]}{.name}={.value}{" secret="}{.valueFrom.secretKeyRef.name}{" key="}{.valueFrom.secretKeyRef.key}{"\n"}{end}' \
  | grep -E '^(MODEL_PATH|NGC_API_KEY)='
```

Both `NEXT_PUBLIC_VST_API_URL` and `NEXT_PUBLIC_VIOS_API_URL` must be present
and equal the generated public `/vst/api` URL. The validation command fails if
either is missing, duplicated, or incorrect; `VSS_PROXY_BASE_URL` alone is not a
successful upload configuration. Complete this check before opening the UI.

Expect the proxy to use
`http://vss-vios-ingress:30888`, the model path to be
`ngc:nim/nvidia/cosmos3-nano-reasoner:bf16-final`, and RT-VLM's `NGC_API_KEY` to
reference `ngc-api` / `NGC_API_KEY`. A key in Cloud Shell alone does not supply
credentials to the running pod.

------

## Task 6: Apply the VSS workshop ingress

### 1. Create the browser ingress manifest

```bash
cd "$WORKSHOP_DIR"
cat > vss-ingress.yaml <<'YAML'
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Browser routes validated with the VSS v3.3.0rc0 LVS profile.
# Substitute <RELEASE_NAME>, <NAMESPACE>, and <EXTERNAL_HOST> before applying.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: <RELEASE_NAME>-vss-ingress
  namespace: <NAMESPACE>
  annotations:
    haproxy.org/timeout-client: 3600s
    haproxy.org/timeout-server: 3600s
    haproxy.org/path-rewrite: |
      ^/storage/(.*) /vst/storage/\1
      ^/storage /vst/storage
spec:
  ingressClassName: haproxy
  rules:
  - host: vss.<EXTERNAL_HOST>.nip.io
    http:
      paths:
      - path: /api/chat
        pathType: Prefix
        backend:
          service:
            name: vss-agent-ui
            port:
              number: 3000
      - path: /api/agent
        pathType: Prefix
        backend:
          service:
            name: vss-agent-ui
            port:
              number: 3000
      - path: /api/vss-chat
        pathType: Prefix
        backend:
          service:
            name: vss-agent-ui
            port:
              number: 3000
      - path: /api/proxy
        pathType: Prefix
        backend:
          service:
            name: vss-agent-ui
            port:
              number: 3000
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: vss-agent
            port:
              number: 8000
      - path: /chat
        pathType: Prefix
        backend:
          service:
            name: vss-agent
            port:
              number: 8000
      - path: /websocket
        pathType: Prefix
        backend:
          service:
            name: vss-agent
            port:
              number: 8000
      - path: /static
        pathType: Prefix
        backend:
          service:
            name: vss-agent
            port:
              number: 8000
      - path: /vst
        pathType: Prefix
        backend:
          service:
            name: vss-vios-ingress
            port:
              number: 30888
      - path: /storage
        pathType: Prefix
        backend:
          service:
            name: vss-vios-ingress
            port:
              number: 30888
      - path: /
        pathType: Prefix
        backend:
          service:
            name: vss-agent-ui
            port:
              number: 3000
  - host: kibana.vss.<EXTERNAL_HOST>.nip.io
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: kibana
            port:
              number: 5601
YAML
```
### 2. Substitute and apply
```bash
sed -e "s/<RELEASE_NAME>/${RELEASE}/g" \
    -e "s/<NAMESPACE>/${NAMESPACE}/g" \
    -e "s/<EXTERNAL_HOST>/${EXTERNAL_HOST}/g" \
    vss-ingress.yaml \
  | kubectl apply -n "$NAMESPACE" -f -
```


### 3. Access the stack

All URLs resolve to the same Azure public IP ($EXTERNAL_HOST) via nip.io:

Copy the url and open on your laptop, **not** jumpbox

>+++echo "Open http://vss.${EXTERNAL_HOST}.nip.io/"+++

You should get a home page for VSS blueprint:

!IMAGE[sqxlpa8f.png](instructions346914/sqxlpa8f.png)


## Notes:
Services are exposed through a single Azure public IP using the HAProxy Kubernetes Ingress controller as a LoadBalancer Service. The UI, VSS Agent (HTTP + WebSocket), VST, and Kibana all share that one IP via nip.io hostnames - no DNS setup needed.

nip.io is a free wildcard DNS service that maps any IP into a hostname . example - vss.4.232.249.46.nip.io resolves to 4.232.249.46, no signup, no DNS records to manage. <anything>.<ip>.nip.io → that IP, always.

The VSS cluster's HAProxy ingress does name-based virtual hosting - one public IP serves multiple Kubernetes services (vss, vst, gradio UIs, etc.), distinguished by the HTTP Host: header. That requires a hostname, not a bare IP.

All URLs resolve to the same Azure public IP ($EXTERNAL_HOST) via nip.io:

| Service | URL |
|---------|-----|
| VSS UI | http://vss.${EXTERNAL_HOST}.nip.io/ |
| VSS Agent HTTP API | http://vss.${EXTERNAL_HOST}.nip.io/api/v1 |
| VSS Agent WebSocket | ws://vss.${EXTERNAL_HOST}.nip.io/websocket |
| VST ingest | http://vss.${EXTERNAL_HOST}.nip.io/vst |
| Kibana | http://kibana.vss.${EXTERNAL_HOST}.nip.io/ |



------

## Task 7: Validate upload, chat, summarization, and reports

### 1. Open the UI and upload a short video

Open the VSS URL printed in Task 6. In **Video Management**, enable video upload
and upload a short H.264 MP4. Confirm that the upload succeeds, the video appears
in the list, and playback works. Record its exact **sensor name**, including any
suffix added by VSS, such as `_1` or `_2`; do not assume the original filename is
the sensor name. Use the displayed sensor name without `.mp4` where applicable.

### 2. Set the chat options and verify text generation

Start a new chat with **LLM Reasoning off** and **VLM Reasoning off**. These
settings apply to both the main and sidebar chat panels. Send **"Hello"** and
wait for a final reply. The intermediate planning output is not the final reply.
Remote model calls can take tens of seconds, and one user turn can include
several calls. The UI's **Thinking...** label alone does not mean the reasoning
option is enabled.

### 3. Verify video retrieval

Start a chat scoped to the uploaded sensor and ask:

> Show the video `<sensor name>`.

Confirm the returned clip plays. A `vst_video_clip` tool completion establishes
retrieval, not summarization. In server logs, a rejected HEAD request followed
by a successful ranged GET (`206`, `video/mp4`) is a successful validation
fallback, not a failed download.

### 4. Summarize the named video

In a new chat scoped to that sensor, keep **LLM Reasoning off** and ask:

> Summarize the video `<sensor name>` and cite the timestamps of the main events.

Expect a summarization tool call, such as `lvs_video_understanding`, followed by
a completed response. Compare the summary with playback. Then ask:

> What changes between the beginning, middle, and end? Mention only things visible in this video.

Check that the response does not invent people, objects, or actions. Turning off
the text-client reasoning option does not disable Cosmos video understanding or
the remote text model's summarization capability.

### 5. Generate a report

Ask:

> Generate a report for `<sensor name>` using video summarization.

Accept or refine the report prompt. Open the Markdown/PDF and playback links.
Confirm the report names the intended sensor and its timestamps match playback.
A successful upload, text greeting, or video retrieval is not sufficient to mark
this step complete.

### 6. Capture validation evidence

During the exercises, confirm all workloads are ready and inspect recent
service activity. Run log-following commands one at a time or in separate Cloud
Shell tabs; press Ctrl+C to stop following a log.

```bash
kubectl get pods -n "$NAMESPACE"
kubectl logs -n "$NAMESPACE" deployment/vss-agent --since=5m --tail=150 --follow
```

```bash
kubectl logs -n "$NAMESPACE" deployment/vss-summarization --since=5m --tail=100 --follow
```

```bash
kubectl logs -n "$NAMESPACE" deployment/vss-rtvi-vlm \
  -c vss-rtvi-vlm --since=5m --tail=100 --follow
```

Have the instructor confirm model requests in the existing APIM diagnostics,
where available, for chat/planning, LVS text merging, and report writing. Record
model alias, HTTP status, and request ID rather than credentials or video
contents. The gateway exposes `x-request-id`, `x-model-alias`,
`x-model-provider`, and `x-gateway-attempts`. Backend model IDs displayed in
responses can differ from the public `NemotronLightning` alias; they do not by
themselves indicate a local model deployment.

Record a final outcome for upload/playback, text chat, video retrieval,
summarization, and report generation separately. Periodic `/health` HTTP 200
messages establish health-check responses, not completion of a user request.

------

## References

- [NVIDIA GPU Operator on Azure AKS](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/microsoft-aks.html)
- [RT-VLM software and GPU requirements](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization/blob/v3.3.0rc0/services/rtvi/rt-vlm/README.md)
- [NVIDIA API keys](https://build.nvidia.com/settings/api-keys)

* [VSS Blueprint Card](https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard) : https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard

* [VSS Github](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization) : https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization

* [VSS Blueprint on AKS](https://github.com/azeltov/vss-msbuild-demo) - Hands-On Workshop Repo: https://github.com/azeltov/vss-msbuild-demo

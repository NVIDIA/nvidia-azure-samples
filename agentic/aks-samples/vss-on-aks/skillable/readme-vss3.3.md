###Please login to the VM using the following credentials:

- Username: +++**@lab.VirtualMachine(Win11).Username**+++

- Password:+++**@lab.VirtualMachine(Win11).Password**+++

###Accessing the Azure Portal

1. [] Please open Edge Browser

1. [] Please naviage to +++**https://portal.azure.com**+++

1. [] Login to Azure using the following credentials:

- Username: +++**@lab.CloudPortalCredential(User1).Username**+++

- When asked for a "Temporary Access Pass", click on the Resources tab, and copy the value from "TAP"

!IMAGE[Screenshot 2025-09-30 at 17.32.45.png](instructions308522/Screenshot 2025-09-30 at 17.32.45.png)
<!--Hidden
- Password: +++**@lab.CloudPortalCredential(User1).Password**+++ -->





# Overview

This workshop guides you through deploying the NVIDIA [Video Search and Summarization (VSS) Long Video Summarization (LVS) blueprint](https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard)  on Azure Kubernetes Service (AKS) using a single Standard_NC96ads_A100_v4 node (4× A100 80 GB GPUs).

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
Cosmos 3 Nano Reasoner in RT-VLM, Nemotron 3.5 Lightning 30B-A3B LLM and NVIDIA NIM. Long videos are routed to the LVS microservice, which
chunks, analyzes and synthesizes results through a Context-Aware RAG pipeline. The results are then passed to the report agent for PDF output.

!IMAGE[6jx4l4s2.png](instructions346914/6jx4l4s2.png)

Services are exposed through one Azure public IP using the **HAProxy Kubernetes Ingress** controller and [`nip.io`](https://nip.io/) hostnames. This update targets VSS `v3.3.0rc0`: deployment, summarization, and report generation were validated on an existing cluster with two NC48ads nodes (four A100 80 GB GPUs total). Creating a fresh cluster and the browser upload control remain to be validated.

# Prerequisites 

- NVIDIA Account and API Key (follow [these instructions](https://nvdam.widen.net/s/tvgjgxrspd/create-build-account-and-api-key) to create an account and generate an API Key)

> [+Hint] Having trouble with your APi key?
> 
> If you have trouble using your key, generate an NGC API key with access to the required models and containers: +++<YOUR_NGC_API_KEY>+++

------
# Task 1: Environment Configuration

### 1. Set up Cloud Shell

1. Open Cloud Shell

Once you log in, click on the Cloud Shell button, located at the top bar:
!IMAGE[Azure_Cloud_Shell.png](instructions291595/Azure_Cloud_Shell.png)


(Note: if it's not visible, either make the Lab window larger or click on the 3 dots):
!IMAGE[Azure_Cloud_Shell_Expand.png](instructions291595/Azure_Cloud_Shell_Expand.png)

2. When asked, select "Bash"
!IMAGE[Bash.png](instructions291595/Bash.png)

3. When asked, select "No Storage", the only available Subscription, and click "Apply"
!IMAGE[Subscription.png](instructions291595/Subscription.png)


### 2. Configure NVIDIA API Key

The first step in the pipeline is to deploy an inference NIM - this will be used to host the LLM that will be responsible for generating responses for a given set of user queries. In order to get started with NIM, we'll need to make sure we have access to an [NVIDIA API key](https://build.nvidia.com/settings/api-keys). We can export this key to be used an environment variable:

>+++export NGC_API_KEY=YOUR NVIDIA API KEY+++


### 3. Set up environment variables

>+++export RESOURCE_GROUP=@lab.CloudResourceGroup(ResourceGroup1).Name+++

<!-- we need to rename teh cluster name to vss-aks -->

>+++export CLUSTER_NAME=rag-demo+++

>+++export CHART_NAME=vss-chart+++

>+++export NAMESPACE=vss+++

>+++export RELEASE="vss-lvs"+++



<!-- >+++export REGION=@lab.CloudResourceGroup(ResourceGroup1).Location+++

>+++export CLUSTER_MACHINE_TYPE=Standard_D32s_v5+++

>+++export NODE_POOL_MACHINE_TYPE=standard_nc96ads_a100_v4+++

>+++export NODE_COUNT=1+++

>+++export CPU_COUNT=2+++
-->


<!-- ### 4. Create AKS cluster

>+++

az group create --name rg-vss-aks --location westeurope

az aks create \
  --resource-group rg-vss-aks \
  --name aks-vss \
  --location westeurope \
  --node-count 1 \
  --node-vm-size Standard_D8s_v5 \
  --generate-ssh-keys \
  --network-plugin azure \
  --enable-managed-identity

az aks nodepool add \
  --resource-group rg-vss-aks \
  --cluster-name aks-vss \
  --name gpupool \
  --node-count 1 \
  --node-vm-size Standard_NC40ads_H100_v5 \
  --labels hardware=gpu \
  --node-osdisk-size 512

az aks get-credentials --resource-group rg-vss-aks --name aks-vss --overwrite-existing+++
-->

### 4. Get AKS cluster credentials

>+++az aks get-credentials --resource-group $RESOURCE_GROUP --name $CLUSTER_NAME+++

<!-- ### 6. Create node pool

>+++az aks nodepool add --resource-group $RESOURCE_GROUP \
    --cluster-name $CLUSTER_NAME \
    --name gpupool \
    --node-count $NODE_COUNT \
    --gpu-driver none \
    --node-vm-size $NODE_POOL_MACHINE_TYPE \
    --node-osdisk-size 2048 \
    --max-pods 110+++
-->
------

# Task 2: NVIDIA GPU Operator Installation

### 1. Add the NVIDIA Helm repository
>+++helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --pass-credentials && helm repo update+++

### 2. Install the GPU Operator

For GPU nodes created with `--gpu-driver none`:

>+++helm install --create-namespace --namespace gpu-operator nvidia/gpu-operator --generate-name+++

If reusing AKS nodes that already provide NVIDIA drivers, the container toolkit,
and the device plugin, keep those components in place. The existing-cluster
validation used GPU Operator `v26.7.1` with `driver.enabled=false`,
`toolkit.enabled=false`, and `devicePlugin.enabled=false`. Reuse an existing
operator installation; do not install a second driver or device plugin.

### 3. Validate the installation 

>+++kubectl get pods -n gpu-operator+++

We need to wait until all pods are in "Running" status and their "Ready" column shows all pods ready (e.g. 1/1, 2/2 etc.)

*note: "Pods named *-validator or *-cache-job will show 0/1 Completed - this is the expected end state, not an error. Wait until all non-Completed pods are Running and Ready."

------
# Task 3: NVIDIA NIM Operator Installation

### 1. Create the NIM Operator namespace

>+++kubectl create namespace nim-operator+++

### 2. Install the NIM Operator

>+++helm upgrade --install nim-operator nvidia/k8s-nim-operator  -n nim-operator --create-namespace --version=3.1.2+++


### 3. Validate the installation 

>+++kubectl get pods -n nim-operator+++

We need to wait until all pods are in "Running" status and their "Ready" column shows all pods ready (e.g. 1/1, 2/2 etc.)
------
# Task 4: Install HAProxy ingress

The VSS LVS profile expects global.externalHost to be set at chart install time - the UI bakes that hostname into its NEXT_PUBLIC_* env vars. We therefore install the ingress controller first, capture the Azure-provisioned LoadBalancer IP, then install VSS using that IP.

The controller is cluster-wide; if one already exists, reuse its IngressClass and LoadBalancer Service instead of installing a second one.

### 1. Download the updated workshop files

```bash
export WORKSHOP_DIR="$HOME/vss-workshop"
mkdir -p "$WORKSHOP_DIR"
cd "$WORKSHOP_DIR"
export WORKSHOP_SOURCE="https://raw.githubusercontent.com/NVIDIA/nvidia-azure-samples/fbb61a6d96d4f1b14d6b66ba00d24a100d924f54/agentic/aks-samples/vss-on-aks"
mkdir -p aks/ingress aks/patches aks/values
curl -fsSL "$WORKSHOP_SOURCE/aks/ingress/haproxy-values.yaml" -o aks/ingress/haproxy-values.yaml
curl -fsSL "$WORKSHOP_SOURCE/aks/patches/nemotron-35-a100-cache-profile.patch" -o aks/patches/nemotron-35-a100-cache-profile.patch
curl -fsSL "$WORKSHOP_SOURCE/aks/values/values-aks-a100.yaml" -o aks/values/values-aks-a100.yaml
```

### 2. Add the HAProxy Helm repository

>+++helm repo add haproxytech https://haproxytech.github.io/helm-charts --force-update && helm repo update haproxytech+++

### 3. Install HAProxy ingress

>+++helm upgrade --install vss-haproxy haproxytech/kubernetes-ingress --version 1.54.2 -n vss-ingress --create-namespace -f "$WORKSHOP_DIR/aks/ingress/haproxy-values.yaml" --wait --timeout 5m+++

>+++kubectl get ingressclass haproxy+++

### 4. Wait for the Azure public IP

>+++kubectl -n vss-ingress get svc vss-haproxy-kubernetes-ingress -w+++

Once the Service has an EXTERNAL-IP, press Ctrl-C to stop watching.

### 5. Get the external Azure public IP

>+++export EXTERNAL_HOST=$(kubectl -n vss-ingress get svc vss-haproxy-kubernetes-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}')+++

>+++test -n "$EXTERNAL_HOST" && echo "EXTERNAL_HOST=$EXTERNAL_HOST"+++

------

## Task 5: Deploy VSS LVS Blueprint

### 1. Check out the tested release and apply the A100 cache patch

Run from the workshop directory configured in Task 4.

```bash
cd "$WORKSHOP_DIR"
git clone --branch v3.3.0rc0 --single-branch \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git
cd video-search-and-summarization

git apply "$WORKSHOP_DIR/aks/patches/nemotron-35-a100-cache-profile.patch"
helm dependency build deploy/helm/services/nims --skip-refresh
helm dependency build deploy/helm/developer-profiles/dev-profile-lvs --skip-refresh
```

The release moved the LVS chart to `deploy/helm/developer-profiles/dev-profile-lvs`.
It uses **Nemotron 3.5 Lightning 30B-A3B** for the LLM and the integrated
**Cosmos 3 Nano Reasoner** checkpoint in RT-VLM. The old `llmNameSlug`,
`vlmNameSlug`, and Cosmos Reason2 A100 KV-cache overrides do not apply.

On A100, the unmodified Nemotron `NIMCache` selects multiple model profiles,
including NVFP4 profiles the hardware cannot serve, into its 100 GiB PVC.
The patch adds an optional `cacheModelProfile` selector; the workshop values
pin it to the INT4 profile also used by the NIMService. This patch was validated
locally with `v3.3.0rc0` and is required until it is included upstream.

The source tag pins the charts. Some RC images use `develop-latest`, so their
contents can change between deployments even with the source tag pinned.

### 2. Create NGC secrets outside Helm values

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
```

The A100 values set `ngc.createSecrets: false`, keeping the key out of Helm
release values. Do not commit keys or rendered Secret manifests.

### 3. Install the LVS chart

```bash
cd deploy/helm/developer-profiles

helm upgrade --install "$RELEASE" ./dev-profile-lvs \
  -f ./dev-profile-lvs/values-lvs.yaml \
  -f "$WORKSHOP_DIR/aks/values/values-aks-a100.yaml" \
  --set global.externalHost="vss.${EXTERNAL_HOST}.nip.io" \
  --set global.kibanaPublicUrl="http://kibana.vss.${EXTERNAL_HOST}.nip.io" \
  -n "$NAMESPACE" --timeout 10m
```

`managed-csi-premium` is the tested Azure StorageClass. Check `kubectl get sc`
and change the workshop value if your cluster uses another class.

### 4. Wait for model cache and workloads

```bash
kubectl get nimcache,nimservice,pvc -n "$NAMESPACE"
kubectl get pods -n "$NAMESPACE"
```

Wait for Nemotron `NIMCache` and `NIMService` to report `Ready`, all PVCs to be
`Bound`, and long-running pods to be `Running` and Ready. First model download
and initialization can take several minutes. The four GPU workloads are
Nemotron NIM, RT-VLM, video summarization, and VIOS stream processing.


## Task 6: Apply the VSS workshop ingress external host to cluster

### 1. Create a custom value vss-ingress.yaml

>+++cd "$WORKSHOP_DIR"+++

>+++vi vss-ingress.yaml+++

When the new screen loads (it will look like an empty screen), press 'i' letter on your keyboard for insert.

Then copy all of the below values, **right click ** in the empty screen on the terminal and click "**paste**"

Then press "**Esc**" on your keyboard, **type ":wq"** and hit "**Enter**"
```
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

## Task 7: Test the VSS blueprint

*  Download a free video from [https://www.pexels.com/videos/](https://www.pexels.com/videos/) or another free stock video website or use your own video and upload and analyze the video using the VSS Blueprint

1. In the UI, upload the clip and wait for its ingestion to finish. Confirm it
   appears in the video list and plays. Record the **sensor name** shown by VSS.
2. Start a new chat scoped to that sensor. Ask: **“Summarize this video and cite
   the timestamps of the main events.”** Compare the response with playback.
3. Ask: **“What changes between the beginning, middle, and end? Mention only
   things visible in this video.”** Check that the answer does not invent people,
   vehicles, or actions absent from the clip.
4. Ask: **“Generate a report for `<sensor name>` using video summarization.”**
   Use the sensor name exactly as VSS displays it, generally without `.mp4`.
   Accept or refine the report prompt, then open the Markdown/PDF and playback
   links. Confirm the report names the intended sensor and timestamps match.

If a report references a different sensor, start a new chat with the correct
sensor selected and repeat the request. Record the UI action, response, and
relevant pod logs if a step fails. The report generation and artifact links
were validated on the A100 deployment; browser upload should be confirmed in
each workshop run.


------

## References:

* [VSS Blueprint Card](https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard) : https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard

* [VSS Github](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization) : https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization

* [VSS Blueprint on AKS](https://github.com/azeltov/vss-msbuild-demo) - Hands-On Workshop Repo: https://github.com/azeltov/vss-msbuild-demo


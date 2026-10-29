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

The VSS 3.0 LVS workflow is a reference agent that generates structured video analysis reports powered by
Cosmos Reason 2 VLM, Nemotron-Nano-9b LLM and NVIDIA NIM. Long videos are routed to the LVS microservice, which
chunks, analyzes and synthesizes results through a Context-Aware RAG pipeline. The results are then passed to the report agent for PDF output.

!IMAGE[6jx4l4s2.png](instructions346914/6jx4l4s2.png)

# Prerequisites

- NVIDIA Account and API Key (follow [these instructions](https://nvdam.widen.net/s/tvgjgxrspd/create-build-account-and-api-key) to create an account and generate an API Key)

> [+Hint] Having trouble with your APi key?
>
> If you have trouble using your key, please use this one: +++<YOUR_NGC_API_KEY>+++

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

>+++helm install --create-namespace --namespace gpu-operator nvidia/gpu-operator --generate-name+++

### 3. Validate the installation

>+++kubectl get pods -n gpu-operator+++

We need to wait until all pods are in "Running" status and their "Ready" column shows all pods ready (e.g. 1/1, 2/2 etc.)

*note: "Pods named *-validator or *-cache-job will show 0/1 Completed - this is the expected end state, not an error. Wait until all non-Completed pods are Running and Ready."

------
# Task 3: NVIDIA NIM Operator Installation

### 1. Create the NIM Operator namespace

>+++kubectl create namespace nim-operator+++

### 2. Install the NIM Operator

>+++helm upgrade --install nim-operator nvidia/k8s-nim-operator  -n nim-operator --create-namespace --version=3.1.0+++


### 3. Validate the installation

>+++kubectl get pods -n nim-operator+++

We need to wait until all pods are in "Running" status and their "Ready" column shows all pods ready (e.g. 1/1, 2/2 etc.)
------
# Task 4: Install ingress-nginx

The VSS LVS profile expects global.externalHost to be set at chart install time - the UI bakes that hostname into its NEXT_PUBLIC_* env vars. We therefore install the ingress controller first, capture the Azure-provisioned LoadBalancer IP, then install VSS using that IP.

### 1. Add the ingress-nginx Helm repository

>+++helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx && helm repo update ingress-nginx+++

### 2. Install ingress-nginx

>+++helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx --version 4.15.1 -n ingress-nginx --create-namespace --set controller.service.type=LoadBalancer --set controller.service.externalTrafficPolicy=Local+++

### 3. Wait for the Azure public IP

>+++kubectl get service --namespace ingress-nginx ingress-nginx-controller --output wide+++

### 4. Get the external Azure public IP

>+++export EXTERNAL_HOST=$(kubectl -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}')+++

>+++echo "EXTERNAL_HOST=$EXTERNAL_HOST"+++

------

## Task 5: Deploy VSS LVS Blueprint

### 1. Clone the repository

```bash
git clone --branch feat/kubernetes-support --single-branch \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git

cd video-search-and-summarization/deployments/helm/developer-profiles
```

### 2. Install the chart

```bash
export STORAGE_CLASS="managed-csi-premium"

helm upgrade --install "$RELEASE" ./dev-profile-lvs \
  -f dev-profile-lvs/values-lvs.yaml \
  -n "$NAMESPACE" --create-namespace \
  --set llmNameSlug=nvidia-nemotron-nano-9b-v2 \
  --set vlmNameSlug=cosmos-reason2-8b \
  --set-string ngc.apiKey="$NGC_API_KEY" \
  --set global.externalHost="vss.${EXTERNAL_HOST}.nip.io" \
  --set global.kibanaPublicUrl="http://kibana.vss.${EXTERNAL_HOST}.nip.io" \
  --set global.storageClass="$STORAGE_CLASS" \
  --set nims.cosmos-reason2-8b.hardwareProfile=A100-80GB \
  --set 'nims.cosmos-reason2-8b.envByHardware.A100-80GB[0].name=NIM_KVCACHE_PERCENT' \
  --set-string 'nims.cosmos-reason2-8b.envByHardware.A100-80GB[0].value=0.9' \
  --set 'nims.cosmos-reason2-8b.envByHardware.A100-80GB[1].name=NIM_DISABLE_MM_PREPROCESSOR_CACHE' \
  --set-string 'nims.cosmos-reason2-8b.envByHardware.A100-80GB[1].value=1'
```

**StorageClass: managed-csi-premium (Premium SSD)**  is recommended - NIM model caches are ~120 GiB each and benefit from premium throughput on first model load.

### 3. Wait for pods to come up

First-time install: NIM model pull from NGC dominates (~15-30 min for ~240 GiB across two models).


>+++kubectl get pods -n $NAMESPACE+++

Move on once pods  are Running/Ready.

*Possible Troubleshooting:
The vss-lvs-lvs-server pod may enter CrashLoopBackOff during initial deployment. This is expected - it depends on the Cosmos VLM NIM, which takes several minutes to fully load. Once vss-lvs-cosmos-reason2-8b-* shows 1/1 Running, delete the LVS server pod . It will come up cleanly within 1-3 minutes.

``` kubectl delete pod -n $NAMESPACE vss-lvs-lvs-server-<hash>```


## Task 6: Apply the VSS workshop ingress external host to cluster

### 1. Create a custom value vss-ingress.yaml

>+++vi vss-ingress.yaml+++

When the new screen loads (it will look like an empty screen), press 'i' letter on your keyboard for insert.

Then copy all of the below values, **right click ** in the empty screen on the terminal and click "**paste**"

Then press "**Esc**" on your keyboard, **type ":wq"** and hit "**Enter**"
```
# Nginx-class Ingress for VSS LVS on AKS.
# Edit the placeholders below (or substitute with `sed`) before applying.
# Paths only cover services that the LVS profile actually deploys.

apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: <RELEASE_NAME>-vss-ingress
  namespace: <NAMESPACE>
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: 2g
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  ingressClassName: nginx
  rules:
  - host: vss.<EXTERNAL_HOST>.nip.io
    http:
      paths:
      - backend:
          service:
            name: <RELEASE_NAME>-vss-ui
            port:
              number: 3000
        path: /api/chat
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vss-agent
            port:
              number: 8000
        path: /api
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vss-agent
            port:
              number: 8000
        path: /chat
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vss-agent
            port:
              number: 8000
        path: /websocket
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vss-agent
            port:
              number: 8000
        path: /static
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vst-ingress-dev
            port:
              number: 30888
        path: /vst
        pathType: Prefix
      - backend:
          service:
            name: <RELEASE_NAME>-vss-ui
            port:
              number: 3000
        path: /
        pathType: Prefix
  - host: kibana.vss.<EXTERNAL_HOST>.nip.io
    http:
      paths:
      - backend:
          service:
            name: <RELEASE_NAME>-kibana
            port:
              number: 5601
        path: /
        pathType: Prefix
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
Services are exposed through a single Azure public IP using the ingress-nginx controller as a LoadBalancer Service. The UI, VSS Agent (HTTP + WebSocket), VST, and Kibana all share that one IP via nip.io hostnames - no DNS setup needed.

nip.io is a free wildcard DNS service that maps any IP into a hostname . example - vss.4.232.249.46.nip.io resolves to 4.232.249.46, no signup, no DNS records to manage. <anything>.<ip>.nip.io → that IP, always.

The VSS cluster's nginx ingress does name-based virtual hosting - one public IP serves multiple Kubernetes services (vss, vst, gradio UIs, etc.), distinguished by the HTTP Host: header. That requires a hostname, not a bare IP.

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

------

## References:

* [VSS Blueprint Card](https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard) : https://build.nvidia.com/nvidia/video-search-and-summarization/blueprintcard

* [VSS Github](https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization) : https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization

* [VSS Blueprint on AKS](https://github.com/azeltov/vss-msbuild-demo) - Hands-On Workshop Repo: https://github.com/azeltov/vss-msbuild-demo

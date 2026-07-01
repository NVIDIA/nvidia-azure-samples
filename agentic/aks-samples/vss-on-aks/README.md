# VSS LVS Blueprint on AKS — Hands-On Workshop

This workshop guides you through deploying the **NVIDIA Video Search and Summarization (VSS) Long Video Summarization (LVS)** blueprint on Azure Kubernetes Service (AKS) using a single `Standard_NC96ads_A100_v4` node (4× A100 80 GB GPUs).

Services are exposed through a single Azure public IP using the **ingress-nginx** controller as a `LoadBalancer` Service. The UI, VSS Agent (HTTP + WebSocket), VST, and Kibana all share that one IP via [`nip.io`](https://nip.io/) hostnames — no DNS setup needed.

---

## Prerequisites

- Azure CLI (`az`) installed and logged in
- `kubectl` and `helm` (3.x) installed
- NGC API key — [generate one here](https://org.ngc.nvidia.com/setup/api-keys)
- Azure subscription with quota for `Standard_NC96ads_A100_v4` (96 vCPUs of `Standard NCADS_A100_v4` family)

---

## Task 1: Environment Configuration

### 1. Install AKS preview extension

```bash
az extension add --name aks-preview
az extension update --name aks-preview
```

### 2. Set environment variables

```bash
export NGC_API_KEY="<YOUR_NGC_API_KEY>"
export SUBSCRIPTION="xxxxx"
export LOCATION="WestEurope"
export RESOURCE_GROUP="rg-vss-build-nc96"
export CLUSTER_NAME="aks-vss-az"
export GPU_NODEPOOL="gpupool"
export GPU_NUM_NODES=1
export GPU_NODE_SIZE="Standard_NC96ads_A100_v4"
export SYSTEM_NODE_SIZE="Standard_D8s_v5"

export NAMESPACE="vss-lvs"
export RELEASE="vss-lvs"
```

### 3. Set active subscription

```bash
az account set --subscription "$SUBSCRIPTION"
```

### 4. Check A100 quota

```bash
az vm list-usage \
  --location "$LOCATION" \
  --query "[?name.value=='StandardNCADSA100v4Family'].{Current:currentValue, Limit:limit}" \
  --output table
```

Ensure `Limit - Current >= 96` (vCPUs for one NC96ads node).

---

## Task 2: Create AKS Cluster

### 1. Create resource group

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"
```

### 2. Create AKS cluster (system node pool)

```bash
az aks create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CLUSTER_NAME" \
  --location "$LOCATION" \
  --node-count 1 \
  --node-vm-size "$SYSTEM_NODE_SIZE" \
  --generate-ssh-keys \
  --network-plugin azure \
  --enable-managed-identity
```

### 3. Add GPU node pool

```bash
az aks nodepool add \
  --resource-group "$RESOURCE_GROUP" \
  --cluster-name "$CLUSTER_NAME" \
  --name "$GPU_NODEPOOL" \
  --node-count "$GPU_NUM_NODES" \
  --node-vm-size "$GPU_NODE_SIZE" \
  --node-osdisk-size 512 \
  --gpu-driver none \
  --labels hardware=gpu gpu-sku=a100 \
  --max-pods 110
```

> `--gpu-driver none` prevents AKS from installing its own GPU extension — the NVIDIA GPU Operator installed in Task 3 manages the driver instead.

### 4. Get cluster credentials

```bash
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CLUSTER_NAME" \
  --overwrite-existing
```

### 5. Verify nodes

```bash
kubectl get nodes -o wide
```

Wait until all nodes reach `Ready` status before proceeding.

---

## Task 3: Install Cluster Prerequisites

### 1. Add NVIDIA Helm repository

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
helm repo update nvidia
```

### 2. Install NVIDIA GPU Operator

```bash
helm install --create-namespace --namespace gpu-operator nvidia/gpu-operator --wait --generate-name

```

### 3. Validate GPU Operator

```bash
kubectl get pods -n gpu-operator
```

Wait until all pods are `Running`. Then verify the GPU node reports allocatable GPUs:

```bash
kubectl get nodes -l agentpool=gpupool \
  -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
```

Expected output: `4` (four A100 GPUs on NC96ads).

### 4. Install NVIDIA NIM Operator

The NIM Operator manages NIM model caching and serving (`NIMCache` / `NIMService` CRDs).

#### Install the NIM Operator on any Kubernetes platform

Use the following steps to install the NVIDIA NIM Operator on your Kubernetes cluster.

Ensure the NVIDIA Helm repo is available (already done in **Task 3.1** for this workshop; run again on a fresh machine or if charts are stale):

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
helm repo update nvidia
```

Create the operator namespace (ignore `AlreadyExists` if you run this twice):

```bash
kubectl create namespace nim-operator
```

Install the operator (`--create-namespace` covers a missing namespace if you skipped the step above):

```bash
helm upgrade --install nim-operator nvidia/k8s-nim-operator \
  -n nim-operator \
  --create-namespace \
  --version=3.1.0
```

### 5. Verify NIM Operator

Optionally, confirm the controller pod is running:

```bash
kubectl get pods -n nim-operator
```

Wait until all pods are `Running` before proceeding.

---

## Task 4: Install ingress-nginx (single public IP)

The VSS LVS profile expects `global.externalHost` to be set at chart install time — the UI bakes that hostname into its `NEXT_PUBLIC_*` env vars. We therefore install the ingress controller **first**, capture the Azure-provisioned LoadBalancer IP, then install VSS using that IP.

> The vss repo README example uses HAProxy with `DaemonSet + hostPort`, which expects node IPs to be routable. On AKS nodes are private by default, so we use **ingress-nginx + `Service.type=LoadBalancer`** — Azure provisions one stable public IP that becomes our single entry point. Ingress-nginx is also the most common ingress on AKS and easier to debug.

### 1. Add the ingress-nginx Helm repository

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update ingress-nginx
```

### 2. Install ingress-nginx

```bash
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --version 4.15.1 \
  -n ingress-nginx --create-namespace \
  --set controller.service.type=LoadBalancer \
  --set controller.service.externalTrafficPolicy=Local
```

> `externalTrafficPolicy=Local` preserves real client IPs in access logs and avoids an extra in-cluster hop. On AKS with a single nginx replica this is fine; for multi-replica deployments add `controller.replicaCount=N` plus a `PodDisruptionBudget` so rolling upgrades don't drop traffic.

### 3. Wait for the Azure public IP

```bash
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller

until kubectl -n ingress-nginx get svc ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' | grep -E '.'; do
  echo "Waiting for LoadBalancer IP..."; sleep 5
done; echo

export EXTERNAL_HOST=$(kubectl -n ingress-nginx get svc ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "EXTERNAL_HOST=$EXTERNAL_HOST"
```

> **If your laptop can't reach `$EXTERNAL_HOST:80` later** (TCP times out from a corp/VPN network), the most likely cause is a specific-IP blocklist hitting *this* allocation. Uninstall and reinstall nginx to get a new IP from the Azure pool — the LoadBalancer service IP is non-sticky: `helm uninstall ingress-nginx -n ingress-nginx && kubectl delete ns ingress-nginx`, then re-run Step 2. In practice this resolves most corp-firewall issues for ephemeral workshop URLs.

---

## Task 5: Deploy VSS LVS Blueprint

### 1. Clone the repository

```bash
git clone --branch feat/kubernetes-support --single-branch \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git

cd video-search-and-summarization/deployments/helm/developer-profiles
```

### 2. Install the chart

Use the inline-set form from the vss repo README so we don't have to edit `values-lvs.yaml`. `EXTERNAL_HOST` from Task 4 is reused; the chart prepends `vss.` for the main host and `kibana.vss.` for Kibana, both resolving to the same Azure public IP through `nip.io`.

We also inject a **custom `A100-80GB` hardware profile** for `cosmos-reason2-8b` — the chart ships profiles for `H100`, `L40S`, and `RTXPRO6000BW`, but none for A100. With chart defaults, cosmos's vLLM engine OOMs on KV cache: it tries to serve at `max_model_len=262144` (256K context), which needs ~36 GiB of KV cache, but defaults leave only ~33 GiB after weights and cudagraphs. Setting `NIM_KVCACHE_PERCENT=0.9` (vLLM `gpu_memory_utilization=0.9`) bumps the budget to 0.9 × 80 GB = 72 GB, leaving ~49 GiB for KV — comfortably above the 36 GiB needed. `nemotron-nano-9b-v2` works fine on chart defaults so we leave its profile empty.

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

> **StorageClass:** `managed-csi-premium` (Premium SSD) is recommended — NIM model caches are ~120 GiB each and benefit from premium throughput on first model load. Run `kubectl get sc` to list available classes; substitute another if your cluster uses one.

> **Re-installs with surviving model cache:** the chart sets `helm.sh/resource-policy: keep` on its `NIMCache` resources, so `helm uninstall` does NOT remove the 120 GiB model PVCs. If you re-install on a cluster that still has them, add `--take-ownership` to the `helm upgrade --install` command above so Helm adopts the existing `NIMCache`/PVC/ConfigMap objects instead of erroring on name collision. This saves 15–30 min of NGC re-download.

### 3. Wait for pods to come up

First-time install: NIM model pull from NGC dominates (~15–30 min for ~240 GiB across two models). Re-install with cached models: ~5–10 min for cosmos cold-start (weight load + `torch.compile` + cudagraph capture for 67 sizes).

```bash
kubectl get pods -n "$NAMESPACE" -w
```

Move on once `lvs-server`, `streamprocessing-ms-dev`, `vss-agent`, `vss-ui`, both `*-nim-*` NIMService pods, Elasticsearch, and Kibana are `Running` / `Ready`.

### 4. Recover lvs-server if it's stuck in CrashLoopBackOff

`lvs-server` probes the cosmos VLM at startup via the OpenAI-compatible `/chat/completions` endpoint. If cosmos isn't Ready yet, lvs-server exits non-zero and K8s applies exponential backoff (capped at 5 min). After ~15 restarts it's already at max backoff, so even once cosmos is finally Ready you may wait up to 5 min for the next retry.

Skip the backoff by deleting the pod — K8s recreates it immediately:

```bash
kubectl -n "$NAMESPACE" delete pod -l app.kubernetes.io/name=lvs-server
```

Confirm:

```bash
kubectl -n "$NAMESPACE" get pods | grep lvs-server
# should show 1/1 Running, 0 restarts within ~90s
```

---

## Task 6: Apply the workshop ingress

[aks/ingress/vss-ingress.yaml](aks/ingress/vss-ingress.yaml) is a workshop-tailored nginx-class Ingress. It derives from the vss repo's `vss-ingress-example.yaml` but is rewritten for ingress-nginx:

- `ingressClassName: nginx` (vs. haproxy in upstream)
- Annotations: `nginx.ingress.kubernetes.io/proxy-body-size: 2g` + `proxy-read-timeout`/`proxy-send-timeout: 3600` — needed for large video uploads and long-lived streaming/WebSocket connections
- Paths only cover services the LVS profile actually deploys (the upstream's `behavior-analytics`, `perception-sdr-alerts`, `nvstreamer-alerts`, `vss-va-mcp`, `video-analytics-api`, `alert-bridge` are for the VSS-alerts profile, not LVS — including them just creates 503-on-miss noise)

The kept routes:

| Hostname | Path | Backend |
|----------|------|---------|
| `vss.<IP>.nip.io` | `/` | vss-ui |
| `vss.<IP>.nip.io` | `/api`, `/chat`, `/websocket`, `/static`, `/api/chat` | vss-agent (HTTP + WS) |
| `vss.<IP>.nip.io` | `/vst` | vst-ingress-dev |
| `kibana.vss.<IP>.nip.io` | `/` | kibana |

### 1. Substitute and apply

Run from the workshop repo root:

```bash
cd /path/to/nvidia-azure-samples/agentic/aks-samples/vss-on-aks

sed -e "s/<RELEASE_NAME>/${RELEASE}/g" \
    -e "s/<NAMESPACE>/${NAMESPACE}/g" \
    -e "s/<EXTERNAL_HOST>/${EXTERNAL_HOST}/g" \
    aks/ingress/vss-ingress.yaml \
  | kubectl apply -n "$NAMESPACE" -f -
```

### 2. Verify

```bash
kubectl get ingress -n "$NAMESPACE"
```

The ingress should show the nginx LB IP under `ADDRESS` (may take 10–20s for nginx-ingress to publish the status):

```
NAME                  CLASS   HOSTS                                              ADDRESS         PORTS   AGE
vss-lvs-vss-ingress   nginx   vss.<IP>.nip.io,kibana.vss.<IP>.nip.io             <EXTERNAL_HOST> 80      30s
```

---

## Task 7: Access the stack

All URLs resolve to the same Azure public IP (`$EXTERNAL_HOST`) via `nip.io`:

| Service | URL |
|---------|-----|
| VSS UI | `http://vss.${EXTERNAL_HOST}.nip.io/` |
| VSS Agent HTTP API | `http://vss.${EXTERNAL_HOST}.nip.io/api/v1` |
| VSS Agent WebSocket | `ws://vss.${EXTERNAL_HOST}.nip.io/websocket` |
| VST ingest | `http://vss.${EXTERNAL_HOST}.nip.io/vst` |
| Kibana | `http://kibana.vss.${EXTERNAL_HOST}.nip.io/` |

Print the workshop URL:

```bash
echo "Open http://vss.${EXTERNAL_HOST}.nip.io/"
```

### In-cluster smoke test

If the URLs don't load from your laptop, the most common cause is a corporate / VPN firewall blocking outbound TCP to specific Azure public IPs. Confirm the stack itself is healthy by hitting nginx from inside the cluster:

```bash
NGINX_CIP=$(kubectl -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.spec.clusterIP}')

kubectl run smoketest --rm -i --restart=Never --image=curlimages/curl:latest -- sh -c "
curl -sS -o /dev/null -w 'UI /            HTTP %{http_code}\n' -H 'Host: vss.${EXTERNAL_HOST}.nip.io'   --max-time 5 http://${NGINX_CIP}/
curl -sS -o /dev/null -w 'VST /vst        HTTP %{http_code}\n' -H 'Host: vss.${EXTERNAL_HOST}.nip.io'   --max-time 5 http://${NGINX_CIP}/vst
curl -sS -o /dev/null -w 'Kibana          HTTP %{http_code}\n' -H 'Host: kibana.vss.${EXTERNAL_HOST}.nip.io' --max-time 5 http://${NGINX_CIP}/
"
```

Expected: UI = `200`, VST = `301`, Kibana = `302`. If those return 200/301/302 in-cluster but your laptop times out on the public IP, the cluster is fine but your network blocked this specific Azure IP. Fix per the note at the end of **Task 4** — reinstall ingress-nginx to get a different IP from the Azure pool.

---

## Cleanup

```bash
# 1) VSS workload — NIMCache and PVCs have `helm.sh/resource-policy: keep`, so they survive helm uninstall.
#    Delete them explicitly to free the 240+ GiB of model cache and namespace.
helm uninstall "$RELEASE" -n "$NAMESPACE"
kubectl delete nimcache --all -n "$NAMESPACE"
kubectl delete pvc --all -n "$NAMESPACE"

# 2) Ingress controller — uninstall releases the Azure Public IP (you won't get the same IP back).
helm uninstall ingress-nginx -n ingress-nginx
kubectl delete namespace "$NAMESPACE" ingress-nginx --ignore-not-found

# 3) Full teardown — destroys AKS cluster, NGC secrets, the Azure LB IP, all PVCs/disks, everything in the RG.
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

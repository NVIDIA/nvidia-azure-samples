# NVIDIA AI-Q 2.2 on Azure Container Apps, Foundry, and Azure AI Search

This sample deploys NVIDIA AI-Q 2.2 on Azure. AI-Q's native Azure AI Search
Knowledge Layer handles ingestion and retrieval; no sample-specific Search
plugin is installed.

## Architecture

| Role | Model | Hosting |
| --- | --- | --- |
| Intent classification, shallow research, summaries, final report writing | Nemotron 3.5 Lightning 30B-A3B NVFP4 v2 | Microsoft Foundry Managed Compute, one A100 |
| Document embeddings (2,048 dimensions) | Nemotron 3 Embed 1B BF16 v1 | Microsoft Foundry Managed Compute, one A100 |
| Clarification and deep research | Nemotron 3 Ultra NVFP4 v1 | Foundry Fireworks, Data Zone Standard pay-per-token |

Lightning and Embed use their official `azure-huggingface` catalog assets and
Microsoft-managed deployment templates. Foundry owns the GPU topology, model
weights, serving runtime, container image, and patching. All three models share
one OpenAI-compatible Foundry endpoint.

AI-Q calls Foundry deployment names directly. A narrow compatibility hook in
the agent image lets NAT 1.8 apply `/think` and `/no_think` to the Lightning
deployment name. The same hook converts the native embedding client's NVIDIA
`input_type` values to the `query:` and `passage:` prefixes expected by
Foundry. No proxy or model-serving container is added.

The AI-Q backend and frontend run on Azure Container Apps.
PostgreSQL stores jobs, events, summaries, and checkpoints. Azure AI Search
stores document chunks and vectors. Key Vault and managed identity provide
secret and data-plane access.

Deep research enables AI-Q 2.2's built-in synthesis writer skill. It instructs
the Lightning writer to publish the final Markdown report with exact verified
source keys, preserving AI-Q's citation-integrity checks for native Search.
The image also applies a narrow AI-Q 2.2 compatibility fix for URL-less sources:
when AI-Q renders a verified document locator as `key: key`, verification checks
the final key first and still requires it to exist in the captured registry.

Fireworks on Foundry is a non-Microsoft service. Prompts and responses are
shared with Fireworks and are outside the Microsoft EU Data Boundary. Review
the [Fireworks on Foundry data terms](https://learn.microsoft.com/azure/foundry/how-to/fireworks/enable-fireworks-models)
before deploying.

## Prerequisites

- Azure CLI 2.60 or newer with the `containerapp` extension
- `curl` and `jq` for endpoint checks
- Permission to create resources and role assignments
- Foundry Managed Compute A100 quota for two model instances
- `Microsoft.CognitiveServices/Fireworks.EnableDeploy` registered
- A Tavily API key for web research
- Bash

## 1. Create a resource group

```bash
az login
az account set --subscription "<SUBSCRIPTION>"

export RG="rg-aiq-workshop"
export LOCATION="westeurope"
az group create --name "$RG" --location "$LOCATION"
```

Run the remaining commands from this sample directory.

## 2. Deploy infrastructure and models

```bash
az deployment group create \
  --resource-group "$RG" \
  --name main \
  --template-file main.bicep \
  --parameters prefix=aiq
```

The sample resources remain in the resource-group region. The Foundry account
defaults to East US because Fireworks `DataZoneStandard` is currently offered
only in supported US regions. Managed Compute uses Foundry's Global deployment
scope. Override `ultraLocation` if availability changes.

The default Fireworks Ultra capacity is 100. Deep research runs concurrent
model calls and was heavily throttled at capacity 10 during validation.
Capacity 100 completed the workload; transient 429 responses can still occur
and are retried by the configured client. Override `ultraCapacity` to tune
throughput and potential pay-per-token spend.

Provisioning the two Managed Compute models normally takes 10–20 minutes.
The template generates the PostgreSQL administrator password and stores it in
Key Vault. Re-running the deployment without supplying `pgAdminPassword`
rotates that password; restart the backend afterward so it receives the new
secret value.

Capture the outputs after deployment:

```bash
read_out () { az deployment group show -g "$RG" -n main --query "properties.outputs.$1.value" -o tsv; }

export ACR_NAME=$(read_out acrName)
export ACR_LOGIN=$(read_out acrLoginServer)
export KV_NAME=$(read_out kvName)
export KV_URI=$(read_out kvUri)
export PG_HOST=$(read_out pgHost)
export PG_ADMIN=$(read_out pgAdminUser)
export DB_JOBS=$(read_out dbJobs)
export DB_CHECKPOINTS=$(read_out dbCheckpoints)
export SEARCH_ENDPOINT=$(read_out searchEndpoint)
export ACA_ENV=$(read_out acaEnvName)
export UAMI_ID=$(read_out uamiId)
export UAMI_CLIENT_ID=$(read_out uamiClientId)
export APPI_CONN_STR=$(read_out appiConnectionString)
export FOUNDRY_ENDPOINT=$(read_out foundryEndpoint)
export AISERVICES_NAME=$(read_out aiServicesName)
```

## 3. Build and import application images

```bash
az acr import \
  --name "$ACR_NAME" \
  --source nvcr.io/nvidia/blueprint/aiq-frontend:2.2.0 \
  --image aiq-frontend:2.2.0

az acr build \
  --registry "$ACR_NAME" \
  --image aiq-agent:2.2.0-azure \
  .
```

## 4. Verify the official Foundry models

```bash
FOUNDRY_KEY=$(az cognitiveservices account keys list \
  --resource-group "$RG" --name "$AISERVICES_NAME" \
  --query key1 -o tsv)

curl -fsS "${FOUNDRY_ENDPOINT}chat/completions" \
  -H "Authorization: Bearer ${FOUNDRY_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"nemotron-3-5-lightning","messages":[{"role":"system","content":"/no_think"},{"role":"user","content":"Reply with ready."}],"max_tokens":256}' \
  | jq '{model, content: .choices[0].message.content}'

curl -fsS "${FOUNDRY_ENDPOINT}embeddings" \
  -H "Authorization: Bearer ${FOUNDRY_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"nemotron-3-embed-1b","input":"query: Azure AI Search"}' \
  | jq '{model, dimensions: (.data[0].embedding | length)}'

curl -fsS "${FOUNDRY_ENDPOINT}chat/completions" \
  -H "Authorization: Bearer ${FOUNDRY_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"nemotron-3-ultra","messages":[{"role":"user","content":"Reply with ready."}],"max_tokens":32}' \
  | jq '{model, content: .choices[0].message.content}'

unset FOUNDRY_KEY
```

## 5. Deploy the AI-Q backend

```bash
export TAVILY_API_KEY="<TAVILY_API_KEY>"

az containerapp create \
  --resource-group "$RG" \
  --name aiq-agent \
  --environment "$ACA_ENV" \
  --image "${ACR_LOGIN}/aiq-agent:2.2.0-azure" \
  --user-assigned "$UAMI_ID" \
  --registry-server "$ACR_LOGIN" \
  --registry-identity "$UAMI_ID" \
  --ingress internal \
  --target-port 8000 \
  --transport http \
  --min-replicas 1 \
  --max-replicas 3 \
  --cpu 1.0 --memory 2.0Gi \
  --secrets \
    "postgres-password=keyvaultref:${KV_URI}secrets/postgres-password,identityref:${UAMI_ID}" \
    "foundry-key=keyvaultref:${KV_URI}secrets/foundry-key,identityref:${UAMI_ID}" \
  --env-vars \
    "TAVILY_API_KEY=$TAVILY_API_KEY" \
    "POSTGRES_PASSWORD=secretref:postgres-password" \
    "FOUNDRY_LIGHTNING_KEY=secretref:foundry-key" \
    "NVIDIA_API_KEY=secretref:foundry-key" \
    "FOUNDRY_ULTRA_KEY=secretref:foundry-key" \
    "PGSSLMODE=require" \
    "NAT_JOB_STORE_DB_URL=postgresql+asyncpg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}" \
    "AIQ_CHECKPOINT_DB=postgresql://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_CHECKPOINTS}?sslmode=require" \
    "AIQ_SUMMARY_DB=postgresql+psycopg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}?sslmode=require" \
    "AZURE_SEARCH_ENDPOINT=${SEARCH_ENDPOINT}" \
    "AZURE_CLIENT_ID=${UAMI_CLIENT_ID}" \
    "AIQ_EMBED_BASE_URL=${FOUNDRY_ENDPOINT%/}" \
    "AIQ_EMBED_MODEL=nvidia/nemotron-3-embed-1b" \
    "AIQ_EMBED_DIM=2048" \
    "AIQ_AZURE_SEARCH_INDEX_PREFIX=aiq" \
    "FOUNDRY_ENDPOINT=${FOUNDRY_ENDPOINT%/}" \
    "CONFIG_FILE=/app/configs/config_web_azure.yml" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=${APPI_CONN_STR}" \
    "LOG_LEVEL=INFO"

export AGENT_FQDN=$(az containerapp show -g "$RG" -n aiq-agent \
  --query properties.configuration.ingress.fqdn -o tsv)
```

## 6. Deploy the frontend

> The command below creates a public, unauthenticated demo UI. Use it only for
> temporary testing, do not upload sensitive data, and delete the resource
> group afterward. For shared or production environments, configure Microsoft
> Entra authentication before enabling external ingress.

```bash
az containerapp create \
  --resource-group "$RG" \
  --name aiq-frontend \
  --environment "$ACA_ENV" \
  --image "${ACR_LOGIN}/aiq-frontend:2.2.0" \
  --user-assigned "$UAMI_ID" \
  --registry-server "$ACR_LOGIN" \
  --registry-identity "$UAMI_ID" \
  --ingress external \
  --target-port 3000 \
  --transport http \
  --min-replicas 1 \
  --max-replicas 3 \
  --cpu 0.5 --memory 1.0Gi \
  --env-vars \
    "BACKEND_URL=https://${AGENT_FQDN}" \
    "REQUIRE_AUTH=false" \
    "FILE_UPLOAD_ACCEPTED_TYPES=.pdf,.docx,.txt,.md" \
    "FILE_EXPIRATION_CHECK_INTERVAL_HOURS=24"

export FRONTEND_FQDN=$(az containerapp show -g "$RG" -n aiq-frontend \
  --query properties.configuration.ingress.fqdn -o tsv)

echo "Open: https://${FRONTEND_FQDN}"
```

## 7. Validate native Azure AI Search

1. Upload a PDF, DOCX, TXT, or Markdown file in the frontend.
2. Confirm an `aiq-*` index appears in Azure AI Search.
3. Ask a question answered only by that document and select Knowledge Base.
4. Confirm the answer contains retrieved evidence and citations.
5. Run a shallow request and confirm Lightning handles it.
6. Run a broad research request and confirm Ultra handles deep research.

Useful diagnostics:

```bash
az containerapp logs show -g "$RG" -n aiq-agent --follow --tail 100
az search service show -g "$RG" -n "$(read_out searchName)" \
  --query "{name:name,status:status}" -o json
```

## Cleanup

Managed Compute A100 deployments bill while running. Delete the resource group
when you no longer need the sample:

```bash
az group delete --name "$RG" --yes --no-wait
```

## References

- [NVIDIA AI-Q 2.2 documentation](https://docs.nvidia.com/aiq-blueprint/2.2.0/)
- [Microsoft Foundry Managed Compute](https://learn.microsoft.com/azure/foundry/concepts/managed-compute-overview)
- [Deploy open-source models with Managed Compute](https://learn.microsoft.com/azure/foundry/how-to/deploy-models-managed)
- [Nemotron 3.5 Lightning](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4)
- [Nemotron 3 Embed 1B](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16)
- [Fireworks models on Foundry](https://learn.microsoft.com/azure/foundry/how-to/fireworks/enable-fireworks-models)

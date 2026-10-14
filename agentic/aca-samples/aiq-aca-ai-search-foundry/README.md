# NVIDIA AI-Q 2.2.1 on Azure Container Apps, Foundry, and Azure AI Search

This sample deploys NVIDIA AI-Q 2.2.1 on Azure. AI-Q's native Azure AI Search
Knowledge Layer handles ingestion and retrieval; no sample-specific Search
plugin is installed. Research uses the uploaded documents through the
Knowledge Base source.

## Architecture

| Role | Model | Hosting |
| --- | --- | --- |
| Intent classification, shallow research, summaries, final report writing | Nemotron 3.5 Lightning 30B-A3B v1 | Foundry Fireworks, Data Zone Standard pay-per-token |
| Document embeddings (2,048 dimensions) | Nemotron 3 Embed 1B BF16 | NVIDIA hosted API |
| Clarification and deep research | Nemotron 3 Ultra NVFP4 v1 | Foundry Fireworks, Data Zone Standard pay-per-token |

Lightning and Ultra use their official Fireworks catalog deployments. Embed
uses its official hosted endpoint on build.nvidia.com. Fireworks chat models
use the Foundry OpenAI endpoint; Embed uses NVIDIA's NIM API endpoint.

AI-Q calls Foundry deployment names directly. NAT's standard OpenAI provider
sends Fireworks-native `reasoning_effort` for Lightning. AI-Q's standard
NVIDIA embedding client sends native `query` and `passage` input types directly
to the NVIDIA endpoint. No compatibility hook, proxy, or model-serving
container is added.

The AI-Q backend and frontend run on Azure Container Apps.
PostgreSQL stores jobs, events, summaries, and checkpoints. Azure AI Search
stores document chunks and vectors. Key Vault and managed identity provide
infrastructure secret and data-plane access. Each participant adds their
NVIDIA API key as an encrypted Container Apps secret during backend deployment.

Deep research enables AI-Q 2.2.1's built-in synthesis writer skill. It instructs
the Lightning writer to publish the final Markdown report with exact verified
source keys, preserving AI-Q's citation-integrity checks for native Search.

## Prerequisites

- Azure CLI 2.60 or newer with the `containerapp` extension
- Permission to create resources and role assignments
- `Microsoft.CognitiveServices/Fireworks.EnableDeploy` registered
- An NVIDIA API key from [build.nvidia.com](https://build.nvidia.com/)

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
only in supported US regions. NVIDIA hosts the global embedding endpoint.
Override `foundryLocation` if availability changes.

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
export FOUNDRY_LIGHTNING_MODEL=$(read_out lightningDeploymentName)
export FOUNDRY_ULTRA_MODEL=$(read_out ultraDeploymentName)
```

## 3. Build and import application images

```bash
az acr import \
  --name "$ACR_NAME" \
  --source nvcr.io/nvidia/blueprint/aiq-frontend:2.2.1 \
  --image aiq-frontend:2.2.1

az acr build \
  --registry "$ACR_NAME" \
  --image aiq-agent:2.2.1-azure \
  .
```

## 4. Discover and verify model endpoints

### 1. Open your Foundry resource

- Open [Microsoft Foundry](https://ai.azure.com) and sign in with your lab Azure account.

- In the **All resources** table, click the resource with type **Foundry** to open its overview pane.

### 2. Inspect the deployed models

- Click **2 model deployments** to see the deployed models.

- Confirm that `NemotronLightning` and `Nemotron3Ultra` are deployed. These names are already set in your backend configuration.

- Return to the Foundry resource overview page.

### 3. Copy the endpoint and API key

- Expand **Endpoints and key** if needed, then copy the **Azure OpenAI endpoint**.

- Copy the **API key** from the same section.

### 4. Export the values in Azure Cloud Shell

Return to your Azure Cloud Shell Bash session. Replace the placeholders below with the values you copied from Foundry.

```bash
export FOUNDRY_ENDPOINT="<copied Azure OpenAI endpoint>"
export FOUNDRY_API_KEY="<copied API key>"

export FOUNDRY_ENDPOINT="${FOUNDRY_ENDPOINT%/}"
export FOUNDRY_ENDPOINT="${FOUNDRY_ENDPOINT%/openai/v1}/openai/v1"
```

The last two lines normalize the endpoint to the OpenAI v1 base URL used by the backend. Both models use the same endpoint and API key.

## 5. Deploy the AI-Q backend

Have your NVIDIA API key ready for document embeddings. The command below
prompts for it without displaying it. Both chat models use the Foundry API key
copied in step 4.

```bash
read -rsp "NVIDIA API key: " NVIDIA_API_KEY && echo

az containerapp create \
  --resource-group "$RG" \
  --name aiq-agent \
  --environment "$ACA_ENV" \
  --image "${ACR_LOGIN}/aiq-agent:2.2.1-azure" \
  --user-assigned "$UAMI_ID" \
  --registry-server "$ACR_LOGIN" \
  --registry-identity "$UAMI_ID" \
  --ingress internal \
  --target-port 8000 \
  --transport http \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 1.0 --memory 2.0Gi \
  --secrets \
    "postgres-password=keyvaultref:${KV_URI}secrets/postgres-password,identityref:${UAMI_ID}" \
    "foundry-key=$FOUNDRY_API_KEY" \
    "nvidia-api-key=$NVIDIA_API_KEY" \
  --env-vars \
    "FOUNDRY_LIGHTNING_KEY=secretref:foundry-key" \
    "NVIDIA_API_KEY=secretref:nvidia-api-key" \
    "FOUNDRY_ULTRA_KEY=secretref:foundry-key" \
    "PGSSLMODE=require" \
    "POSTGRES_PASSWORD=secretref:postgres-password" \
    "NAT_JOB_STORE_DB_URL=postgresql+asyncpg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}" \
    "AIQ_CHECKPOINT_DB=postgresql://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_CHECKPOINTS}?sslmode=require" \
    "AIQ_SUMMARY_DB=postgresql+psycopg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}?sslmode=require" \
    "AZURE_SEARCH_ENDPOINT=${SEARCH_ENDPOINT}" \
    "AZURE_CLIENT_ID=${UAMI_CLIENT_ID}" \
    "AIQ_EMBED_BASE_URL=https://integrate.api.nvidia.com/v1" \
    "AIQ_EMBED_MODEL=nvidia/nemotron-3-embed-1b" \
    "AIQ_EMBED_DIM=2048" \
    "AIQ_AZURE_SEARCH_INDEX_PREFIX=aiq221" \
    "FOUNDRY_ENDPOINT=${FOUNDRY_ENDPOINT%/}" \
    "CONFIG_FILE=/app/configs/config_web_azure.yml" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=${APPI_CONN_STR}" \
    "LOG_LEVEL=WARNING"

export AGENT_FQDN=$(az containerapp show -g "$RG" -n aiq-agent \
  --query properties.configuration.ingress.fqdn -o tsv)
```

Check that the API starts and no model, database, credential, or Search initialization error appears. Startup alone does not prove ingestion or research works; Task 7 tests those paths. Press Ctrl+C to stop following logs.

>+++az containerapp logs show -g "$RG" -n aiq-agent --follow --tail 100+++

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
  --image "${ACR_LOGIN}/aiq-frontend:2.2.1" \
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

Open the displayed frontend URL.

## 7. Validate native Azure AI Search

1. Upload a PDF, DOCX, TXT, or Markdown file in the frontend and wait for ingestion to finish.
2. Confirm an `aiq221-*` index appears in Azure AI Search.
3. Ask a question answered only by that document and select Knowledge Base.
4. Confirm the answer contains retrieved evidence and citations.
5. Run a shallow request and confirm Lightning handles it.
6. Run a detailed research request answerable from the uploaded documents and confirm Ultra handles deep research.
7. Confirm the final report cites the uploaded documents. Knowledge Base is the only source in this sample.

Useful diagnostics:

```bash
az containerapp logs show -g "$RG" -n aiq-agent --follow --tail 100
az search service show -g "$RG" -n "$(read_out searchName)" \
  --query "{name:name,status:status}" -o json
```

## Local validation

```bash
az bicep build --file main.bicep --outfile /tmp/aiq-sample.arm.json
```

Bicep compilation checks infrastructure syntax. Model endpoint checks in step 4
and the document ingestion, retrieval, and deep-research exercises in step 7
remain required before publishing a workshop revision.

## Cleanup

Delete the resource group when you no longer need the sample:

```bash
az group delete --name "$RG" --yes --no-wait
```

## References

- [NVIDIA AI-Q 2.2.1 documentation](https://docs.nvidia.com/aiq-blueprint/2.2.1/)
- [Nemotron 3.5 Lightning on Fireworks](https://fireworks.ai/models/fireworks/nemotron-lightning-3p5-30b-a3b)
- [Nemotron 3 Embed 1B API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-embed-1b-infer)
- [Fireworks models on Foundry](https://learn.microsoft.com/azure/foundry/how-to/fireworks/enable-fireworks-models)

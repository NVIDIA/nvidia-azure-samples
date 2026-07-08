# NVIDIA AIQ Blueprint with Nemotron on ACA, Microsoft Foundry and Azure Search

This workshop walks you through deploying the **NVIDIA AI-Q research agent** end-to-end on Azure-native services. You'll provision the supporting infrastructure with a single Bicep template, fill an Azure Container Registry with the agent and frontend images, wire the agent to Foundry-hosted NIM models and a serverless `gpt-oss-120b` deployment, and connect it to Azure AI Search through a custom Knowledge Layer adapter — all **without modifying AI-Q's source code**.

The adapter ([`aiq-azure-ai-search`](aiq-azure-ai-search/)) self-registers via AI-Q's `nat.plugins` entry point, exposing a new `azure_ai_search_retrieval` workflow type that the agent uses for document ingestion and retrieval. AI-Q stays stock; everything Azure-specific lives in the adapter and the workflow config.

> **Note on the lab version.** If you ran this in a hosted lab environment, the Azure infrastructure (Postgres, AI Search, Foundry hub + project, NIM deployments, ACR, Key Vault, managed identity, observability) was pre-provisioned for you and you started at the image build step. This GitHub version adds the full infrastructure provisioning so you can run the whole thing in your own subscription.

---

## Architecture

Three model endpoints feed the agent:

| Role | Model | Hosting |
| --- | --- | --- |
| Chat / intent / summary | `Nemotron-3-Nano` | NIM on an Azure ML managed online endpoint (A100) |
| Embeddings (2048-dim) | `Llama-3.2-NV-embedqa-1b-v2` | NIM on an Azure ML managed online endpoint (A100) |
| Orchestrator / planner | `gpt-oss-120b` | Azure AI Services GlobalStandard (per-token, serverless) |

Two Azure Container Apps run the workload: an **internal-only agent** (the AI-Q backend) and an **external frontend** (the chat UI). Postgres Flexible Server holds the job store and checkpoints; Azure AI Search holds the document index; Key Vault holds every secret; a user-assigned managed identity (UAMI) carries all RBAC so nothing uses passwords or admin users at the registry or vault.

---

## Prerequisites

- **Azure CLI** 2.57+ (`az --version`) and the Bicep CLI (`az bicep install`)
- An **Azure subscription** where you can create resource groups and assign roles
- **GPU quota:** 48 vCPUs of `Standard NCADSA100v4` family in your chosen region (two `Standard_NC24ads_A100_v4` endpoints, 1×A100 each)
- **`gpt-oss-120b` availability** in your chosen region as a GlobalStandard (serverless) deployment, plus one-time Marketplace terms acceptance on first use in a subscription
- A **Tavily API key** for the web-search tool — free tier at [tavily.com](https://app.tavily.com/) gives 1,000 searches/month
- `git` to clone this repository
- A **bash shell** — all commands below use bash syntax (`export`, `$(...)`, `\` line continuations).

> **Region.** Pick a region that has both A100 quota and `gpt-oss-120b` GlobalStandard availability — not every region offers both. Every resource follows the resource group's location, so all resources land wherever you create the RG. The NIMs consume Azure ML's *own* GPU vCPU quota (separate from raw VM quota); check it under *Portal → Subscriptions → Usage + quotas → Provider: Machine Learning* and file a request if `Limit − Current < 48`. Confirm `gpt-oss-120b` GlobalStandard is offered in the same region via the Foundry model catalog before you deploy.

---

## Task 1: Clone and configure

### 1. Log in and select your subscription

```bash
az login
az account set --subscription "<YOUR_SUBSCRIPTION>"
```

### 2. Clone this repository

```bash
git clone https://github.com/NVIDIA/nvidia-azure-samples.git
cd nvidia-azure-samples/agentic/aca-samples/aiq-aca-ai-search-foundry
```

The sample directory contains everything the build and deploy need: `main.bicep`, `Dockerfile`, `config_web_azure.yml`, and the `aiq-azure-ai-search/` adapter package.

### 3. Create the resource group

```bash
export RG="rg-aiq-workshop"
export LOCATION="<YOUR_REGION>"

az group create --name "$RG" --location "$LOCATION"
```

### 4. Add the Container Apps CLI extension

The only non-core extension this workshop needs is `containerapp` (used in Tasks 6–7):

```bash
az extension add --name containerapp --upgrade
```

---

## Task 2: Provision the infrastructure (Bicep)

This is the part that is pre-deployed for you in the hosted lab. The template provisions Log Analytics + App Insights, a user-assigned managed identity with all RBAC bindings, Key Vault (with a securely generated Postgres admin password pre-loaded), an empty ACR, Postgres Flexible Server with the `aiq_jobs` and `aiq_checkpoints` databases, Azure AI Search (Basic), a Container Apps environment, a Foundry hub + project, both NIM online endpoints, and the `gpt-oss-120b` serverless deployment. The NIM and gpt-oss keys are written into Key Vault automatically.

### 1. Deploy the template

```bash
az deployment group create \
  --resource-group "$RG" \
  --name "main" \
  --template-file main.bicep \
  --parameters prefix=aiq
```

The two NIM online endpoints take ~10–15 minutes each to come up, so a fresh deploy runs **~25–30 minutes** total. The Postgres admin password is securely generated inside the template and stored in the `postgres-password` Key Vault secret — you never pass it as a parameter. Re-deploying without an explicit `pgAdminPassword` value rotates the password.

> **First-time `gpt-oss-120b` deploy.** If the deployment fails with `MarketplaceTermsNotAccepted`, accept the model's Marketplace terms once via the Azure portal (open the `gpt-oss-120b` model card and click **Deploy**), then re-run the command above. Bicep is idempotent, so re-running is safe.

### 2. Confirm what landed

```bash
az resource list -g "$RG" --query "[].{name:name, type:type}" -o table
```

---

## Task 3: Capture the environment

The template emits every value the deploy needs as a deployment output. Pull them into environment variables in one shot:

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

# Model endpoints. NIM scoring URIs end in /score; the OpenAI-compatible
# client needs the /v1 form, so strip /score and append /v1.
NEMO_SCORING=$(read_out nemotronScoringUri)
EMBED_SCORING=$(read_out embedqaScoringUri)
export FOUNDRY_LLM_ENDPOINT="${NEMO_SCORING%/score}/v1"
export FOUNDRY_EMBED_ENDPOINT="${EMBED_SCORING%/score}/v1"
export GPT_OSS_ENDPOINT=$(read_out gptOssEndpoint)   # already ends in /models
```

### Verify

```bash
echo "ACR:      $ACR_LOGIN"
echo "KV:       $KV_NAME"
echo "Search:   $SEARCH_ENDPOINT"
echo "ACA:      $ACA_ENV"
echo "UAMI:     $UAMI_CLIENT_ID"
echo "Nemotron: $FOUNDRY_LLM_ENDPOINT"
echo "embedqa:  $FOUNDRY_EMBED_ENDPOINT"
echo "gpt-oss:  $GPT_OSS_ENDPOINT"
```

All eight lines should be non-empty. The `gpt-oss` endpoint **must end in `/models`** — the OpenAI client appends `/chat/completions` itself at request time.

> **Where the model keys are.** You won't see the NIM or gpt-oss keys here — they're already in Key Vault as `foundry-llm-key`, `foundry-embed-key`, and `gpt-oss-key`, alongside `postgres-password`. The agent reads them at runtime via `keyvaultref` using the managed identity, so no secret is ever printed to your terminal or baked into a command.

---

## Task 4: Build the container images in ACR

The Bicep left you an empty registry. Fill it with two images:

- **`aiq-frontend:2.1.0`** — imported as-is from `nvcr.io` (no customization)
- **`aiq-agent:2.1.0-azure`** — built on the upstream agent image with the `aiq-azure-ai-search` adapter and `config_web_azure.yml` baked in

### 1. Import the frontend image

```bash
az acr import \
  --name "$ACR_NAME" \
  --source nvcr.io/nvidia/blueprint/aiq-frontend:2.1.0 \
  --image aiq-frontend:2.1.0
```

Takes ~1–3 minutes. The `nvcr.io/nvidia/blueprint` images are anonymously pullable.

### 2. Build the custom agent image

From the sample directory (which holds `Dockerfile`, `config_web_azure.yml`, and `aiq-azure-ai-search/`):

```bash
az acr build \
  --registry "$ACR_NAME" \
  --image aiq-agent:2.1.0-azure \
  .
```

Takes ~5–10 minutes. `az acr build` tarballs the current directory into the build context, uploads it to a build agent in ACR, runs the `Dockerfile` there (`FROM nvcr.io/nvidia/blueprint/aiq-agent:2.1.0` + `pip install ./aiq-azure-ai-search` + the config), and pushes the result. Logs stream to your terminal.

### 3. Confirm both images are present

```bash
az acr repository list --name "$ACR_NAME" -o tsv
```

Expected output: `aiq-agent` and `aiq-frontend`.

---

## Task 5: Add your Tavily key

The workshop's web-search tool uses [Tavily](https://tavily.com/). Sign up (Google/GitHub SSO works), copy your API key (starts with `tvly-`), and export it:

```bash
export TAVILY_API_KEY="<PASTE-TAVILY-KEY>"
```

---

## Task 6: Deploy the agent

The command is long by design — every flag wires up one piece of the stack. Secrets are pulled from Key Vault via `keyvaultref` (using the managed identity); endpoints and non-secret config come in as env vars.

```bash
az containerapp create \
  --resource-group "$RG" \
  --name "aiq-agent" \
  --environment "$ACA_ENV" \
  --image "${ACR_LOGIN}/aiq-agent:2.1.0-azure" \
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
      "foundry-llm-key=keyvaultref:${KV_URI}secrets/foundry-llm-key,identityref:${UAMI_ID}" \
      "foundry-embed-key=keyvaultref:${KV_URI}secrets/foundry-embed-key,identityref:${UAMI_ID}" \
      "gpt-oss-key=keyvaultref:${KV_URI}secrets/gpt-oss-key,identityref:${UAMI_ID}" \
  --env-vars \
      "TAVILY_API_KEY=$TAVILY_API_KEY" \
      "POSTGRES_PASSWORD=secretref:postgres-password" \
      "FOUNDRY_LLM_KEY=secretref:foundry-llm-key" \
      "FOUNDRY_EMBED_KEY=secretref:foundry-embed-key" \
      "GPT_OSS_KEY=secretref:gpt-oss-key" \
      "PGSSLMODE=require" \
      "NAT_JOB_STORE_DB_URL=postgresql+asyncpg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}" \
      "AIQ_CHECKPOINT_DB=postgresql://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_CHECKPOINTS}?sslmode=require" \
      "AIQ_SUMMARY_DB=postgresql+psycopg://${PG_ADMIN}:\$(POSTGRES_PASSWORD)@${PG_HOST}:5432/${DB_JOBS}?sslmode=require" \
      "AZURE_SEARCH_ENDPOINT=${SEARCH_ENDPOINT}" \
      "AZURE_CLIENT_ID=${UAMI_CLIENT_ID}" \
      "FOUNDRY_LLM_ENDPOINT=${FOUNDRY_LLM_ENDPOINT}" \
      "FOUNDRY_EMBED_ENDPOINT=${FOUNDRY_EMBED_ENDPOINT}" \
      "GPT_OSS_ENDPOINT=${GPT_OSS_ENDPOINT}" \
      "CONFIG_FILE=/app/configs/config_web_azure.yml" \
      "APPLICATIONINSIGHTS_CONNECTION_STRING=${APPI_CONN_STR}" \
      "LOG_LEVEL=INFO"
```

### 1. Capture the agent's internal FQDN

```bash
export AGENT_FQDN=$(az containerapp show -g "$RG" -n "aiq-agent" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo "Backend URL (internal-only): https://${AGENT_FQDN}"
```

### 2. (Optional) Tail the agent logs while it boots

```bash
az containerapp logs show -g "$RG" -n aiq-agent --follow --tail 50
```

You should see, in order:

1. `aiq_azure_ai_search` plugin discovered via the `nat.plugins` entry point
2. `AzureAISearchRetriever initialized` and `AzureAISearchIngestor initialized`
3. `Application startup complete.` and `Uvicorn running on http://0.0.0.0:8000`

Hit **Ctrl-C** once you see step 3.

---

## Task 7: Deploy the frontend

The frontend is the upstream NVIDIA image with no customization — just env-var configuration pointing it at the internal agent.

```bash
az containerapp create \
  --resource-group "$RG" \
  --name "aiq-frontend" \
  --environment "$ACA_ENV" \
  --image "${ACR_LOGIN}/aiq-frontend:2.1.0" \
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
```

### Capture the public URL

```bash
export FRONTEND_FQDN=$(az containerapp show -g "$RG" -n "aiq-frontend" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo "Open: https://${FRONTEND_FQDN}"
```

---

## Task 8: Test the AI-Q agent

Open the frontend URL in your browser. Optionally tail the agent logs in a side terminal so you can watch the pipeline fire:

```bash
az containerapp logs show -g "$RG" -n aiq-agent --follow --tail 100
```

### 1. Upload a document

Click the document-upload button in the chat UI and upload a PDF — for example the NVIDIA Nemotron 3 white paper. On upload the document is parsed, chunked, embedded via the embedqa NIM, and pushed to Azure AI Search.

### 2. Query 1 — shallow research

> *"What RL algorithm is used for post-training, and what modification did NVIDIA apply to it?"*

The shallow path: the query embeds via the embedqa NIM, runs hybrid + semantic search against AI Search, and `gpt-oss-120b` synthesises a focused answer with inline citations. A quick, single-pass confirmation that the full RAG pipeline works end-to-end.

### 3. Query 2 — deep research (the headline test)

> *"Produce a detailed report on NVIDIA's NVFP4 training recipe for Nemotron 3. Include the format specification (block scaling, E4M3/E2M1, RHTs, stochastic rounding), every layer kept in higher precision and the justification for each, the loss-gap evidence at A3B vs. A8B scale, and the downstream evaluation results. Conclude with what this recipe implies for training other hybrid architectures in FP4."*

This kicks off the deep-research workflow: the orchestrator plans a multi-section report, fires multiple retrieval + reasoning loops (each embedding queries via the embedqa NIM, searching AI Search, and reasoning with `gpt-oss-120b`), and assembles a long, structured report with inline citations. It exercises the whole stack — the Postgres job store, checkpointing, and every model endpoint — and takes a few minutes to complete.

You've just deployed a production-shape AI-Q agent on Azure-native services.

---

## Cleanup

```bash
# Tear down everything in the resource group — Container Apps, NIM endpoints,
# gpt-oss deployment, Postgres, AI Search, ACR, Key Vault, Foundry hub/project.
az group delete --name "$RG" --yes --no-wait
```

> The two NIM online endpoints and the `gpt-oss-120b` GlobalStandard deployment are the only resources that bill while idle (A100 reservations and per-token capacity respectively). Deleting the resource group releases all of them. The Key Vault has soft-delete enabled with a 7-day retention; purge it explicitly if you intend to immediately recreate one with the same name.

---

## References

Go deeper on the pieces you just built:

- [NVIDIA Azure Samples](../../..) — this sample lives here, with the `aiq-azure-ai-search` adapter source, `Dockerfile`, `config_web_azure.yml`, and `main.bicep`.
- [Adding a Data Source — NVIDIA AI-Q Blueprint](https://docs.nvidia.com/aiq-blueprint/2.1.0/extending/adding-a-data-source.html) — the pattern behind the custom Knowledge Layer adapter (`aiq_azure_ai_search`).
- [Add a Specialized Deep Research Skill to Agent Harnesses](https://developer.nvidia.com/blog/add-a-specialized-deep-research-skill-to-agent-harnesses/) — a next step: expose an AI-Q server like this one as a reusable "skill" that agent harnesses (Claude Code, Codex, LangChain) can call to delegate research.
- [NVIDIA AI-Q Blueprint documentation](https://docs.nvidia.com/aiq-blueprint/2.1.0/) — full docs for the blueprint this workshop is based on.

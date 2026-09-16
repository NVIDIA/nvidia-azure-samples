# Switchyard Hands-on Lab

This sample begins by creating a Microsoft Foundry resource, a Foundry project,
an Azure Container Apps environment, a user-assigned managed identity, and a
Log Analytics workspace with Bicep. The Container Apps environment uses the
Consumption workload profile and sends application logs to Log Analytics. The
user-assigned identity has the Foundry User role on the Foundry resource so a
container app can invoke its model deployments without API keys. Microsoft
Entra ID authentication is required by default.

## Prerequisites

- An Azure subscription
- Azure CLI with Bicep support
- Permission to create resources in an Azure resource group
- Permission to create Azure role assignments, such as Role Based Access
  Control Administrator, User Access Administrator, or Owner
- A [region that supports Microsoft Foundry](https://learn.microsoft.com/azure/ai-foundry/reference/region-support)

## Sign In to Azure

Run all commands from this sample directory:

```bash
cd inference/aca/switchyard_hol

az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
```

Register the Azure Cognitive Services, Azure Container Apps, managed identity,
and Log Analytics resource providers. Registration might require
subscription-level permission.

```bash
az provider register \
  --namespace Microsoft.CognitiveServices \
  --wait

az provider register \
  --namespace Microsoft.App \
  --wait

az provider register \
  --namespace Microsoft.ManagedIdentity \
  --wait

az provider register \
  --namespace Microsoft.OperationalInsights \
  --wait
```

## Create a Resource Group

Choose a supported region and create a resource group:

```bash
export RESOURCE_GROUP="rg-switchyard-hol"
export LOCATION="<AZURE_REGION>"

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"
```

## Deploy the Lab Infrastructure

Deploy the Foundry resource, its default project, the Container Apps
environment, and its Log Analytics workspace:

```bash
az deployment group create \
  --name "deploy-switchyard-foundry" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters \
    location="$LOCATION" \
    namePrefix="switchyard"
```

The template appends a deterministic suffix to `namePrefix` so that resource
names are repeatable within the resource group and the Foundry resource's
custom domain is globally unique. The deployment outputs the Foundry and
Container Apps environment, managed identity, and Log Analytics workspace names
and resource IDs, along with the Foundry project endpoint. Log Analytics
retains logs for 30 days by default; set the `logRetentionDays` deployment
parameter to use a value from 30 through 730 days.

Retrieve the project endpoint after deployment:

```bash
az deployment group show \
  --name "deploy-switchyard-foundry" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.outputs.projectEndpoint.value \
  --output tsv
```

Open [Microsoft Foundry](https://ai.azure.com/) and select the created project.
This step creates the Foundry resource, project, and an empty Container Apps
environment connected to Log Analytics. It does not deploy a model or a
container app, so the workspace does not receive Container Apps application
logs until a container app is added in a later step.

## Open a Debug Container Console

Azure Container Apps does not expose conventional network SSH. The supported
SSH-like experience is an interactive console opened with `az containerapp
exec`.

Install or update the Container Apps CLI extension, retrieve the environment
and user-assigned identity details from the Bicep deployment outputs, and
create a debug app with one always-running replica. The Azure CLI image includes
`az`, `curl`, Bash, and other common command-line tools.

```bash
az extension add --name containerapp --upgrade

export CONTAINER_APPS_ENVIRONMENT="$(
  az deployment group show \
    --name "deploy-switchyard-foundry" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppsEnvironmentName.value \
    --output tsv
)"
export CONTAINER_APP_IDENTITY_ID="$(
  az deployment group show \
    --name "deploy-switchyard-foundry" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppIdentityResourceId.value \
    --output tsv
)"
export CONTAINER_APP_IDENTITY_CLIENT_ID="$(
  az deployment group show \
    --name "deploy-switchyard-foundry" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppIdentityClientId.value \
    --output tsv
)"
export FOUNDRY_NAME="$(
  az deployment group show \
    --name "deploy-switchyard-foundry" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.foundryName.value \
    --output tsv
)"
export DEBUG_APP_NAME="switchyard-debug"

az containerapp create \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --environment "$CONTAINER_APPS_ENVIRONMENT" \
  --user-assigned "$CONTAINER_APP_IDENTITY_ID" \
  --image "mcr.microsoft.com/azure-cli:azurelinux3.0" \
  --workload-profile-name "Consumption" \
  --command /bin/sleep \
  --args 86400 \
  --env-vars \
    "AZURE_CLIENT_ID=$CONTAINER_APP_IDENTITY_CLIENT_ID" \
    "FOUNDRY_NAME=$FOUNDRY_NAME" \
  --cpu 2.0 \
  --memory 4Gi \
  --min-replicas 1 \
  --max-replicas 1
```

Confirm that the user-assigned identity is attached to the app:

```bash
az containerapp identity show \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output jsonc
```

Switchyard compiles from source and requires 2 vCPU and 4 GiB for this lab.
Confirm the resource allocation before opening the shell:

```bash
az containerapp show \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.template.containers[0].resources' \
  --output yaml
```

If the app was created earlier with smaller limits, update it and wait for the
new revision before reconnecting:

```bash
az containerapp update \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --cpu 2.0 \
  --memory 4Gi \
  --min-replicas 1 \
  --max-replicas 1
```

The system-assigned identities on the Foundry resource and project belong to
those resources; they do not authenticate the Container App. The app uses the
separate user-assigned identity created by this template. That identity's
Foundry User assignment is scoped to the Foundry account and supports
Microsoft Entra ID authentication to the account-level
`services.ai.azure.com/openai/v1` endpoint.

Open an interactive Bash shell. Press `Ctrl-D` or run `exit` to close it:

```bash
az containerapp exec \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --command /bin/bash
```

Inside the container, confirm that the managed identity endpoint and selected
user-assigned identity are available:

```bash
test -n "$IDENTITY_ENDPOINT" && \
test -n "$IDENTITY_HEADER" && \
test -n "$AZURE_CLIENT_ID" && \
echo "User-assigned managed identity is available."
```

Request a short-lived Microsoft Entra token for Foundry. The `client_id`
parameter selects the user-assigned identity; omitting it makes Azure look for a
system-assigned identity instead.

```bash
TOKEN_RESPONSE="$(
  curl --fail-with-body --silent --show-error \
    --get "$IDENTITY_ENDPOINT" \
    --header "X-IDENTITY-HEADER: $IDENTITY_HEADER" \
    --data-urlencode "resource=https://cognitiveservices.azure.com" \
    --data-urlencode "api-version=2019-08-01" \
    --data-urlencode "client_id=$AZURE_CLIENT_ID"
)"

export AZURE_AI_AUTH_TOKEN="$(
  printf '%s' "$TOKEN_RESPONSE" |
    sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
)"

if [ -n "$AZURE_AI_AUTH_TOKEN" ]; then
  echo "Foundry access token acquired."
else
  echo "Managed identity token request failed:"
  printf '%s\n' "$TOKEN_RESPONSE"
fi
```

This token flow uses only the shell tools already present in the debug image.
Do not print the access token itself. Variables exported in an interactive
console do not carry over to a new `az containerapp exec` session or a new
Container App revision. Repeat this token-acquisition step after every
reconnection before starting Switchyard.

Set the name of a model deployment that already exists in the Foundry account,
then test the OpenAI-compatible chat-completions endpoint:

```bash
export MODEL_DEPLOYMENT_NAME="<MODEL_DEPLOYMENT_NAME>"

curl --fail-with-body --silent --show-error \
  --request POST \
  "https://${FOUNDRY_NAME}.services.ai.azure.com/openai/v1/chat/completions" \
  --header "Authorization: Bearer $AZURE_AI_AUTH_TOKEN" \
  --header "Content-Type: application/json" \
  --data "{
    \"model\": \"$MODEL_DEPLOYMENT_NAME\",
    \"messages\": [
      {
        \"role\": \"user\",
        \"content\": \"Reply with: Switchyard Foundry endpoint is working.\"
      }
    ]
  }"
```

Role assignments can take several minutes to propagate. If the token succeeds
but the inference request is initially denied, wait a few minutes and try it
again.

## Install and Configure Switchyard

Continue in the Container App Bash shell. [Switchyard](https://github.com/NVIDIA-NeMo/Switchyard)
is pre-1.0 software, so this lab pins the standalone server to version `0.2.0`.
The server is built from Rust source and can take several minutes to install.

Install the native build tools and Rust, then install Switchyard:

```bash
tdnf install -y \
  binutils \
  cmake \
  gcc \
  gcc-c++ \
  glibc-devel \
  kernel-headers \
  make \
  perl

curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
  https://sh.rustup.rs |
  sh -s -- -y --profile minimal --default-toolchain 1.96.1

. "$HOME/.cargo/env"
export CARGO_BUILD_JOBS=1

cargo install --locked --version 0.2.0 switchyard-server
switchyard-server --help >/dev/null
echo "Switchyard installed."
```

If `rustc` exits with signal 9 (`SIGKILL`), the container ran out of memory.
Run `cat /sys/fs/cgroup/memory.max`; a 4 GiB limit is reported as `4294967296`.
If the value is lower, exit the console, resize the app with the command above,
and reconnect before retrying the installation.

Set the deployment names for an efficient and a capable model in the same
Foundry account. These are the deployment names sent in the `model` field, not
the model catalog names. Both deployments must accept OpenAI-compatible Chat
Completions requests.

```bash
export EFFICIENT_MODEL_DEPLOYMENT="<EFFICIENT_MODEL_DEPLOYMENT_NAME>"
export CAPABLE_MODEL_DEPLOYMENT="<CAPABLE_MODEL_DEPLOYMENT_NAME>"
```

Create `/tmp/routes.toml`. The Foundry account exposes both deployments through
one OpenAI-compatible base URL. `api_key_env` tells Switchyard to read the
current managed-identity token from the environment instead of storing it in
the configuration file. See the Switchyard
[TOML schema](https://github.com/NVIDIA-NeMo/Switchyard/blob/v0.2.0/docs/reference/toml_schema.md)
for all available client, target, and route settings.

```bash
export SWITCHYARD_CONFIG="/tmp/routes.toml"

cat > "$SWITCHYARD_CONFIG" <<TOML
schema_version = 1

[llm_clients.foundry]
format = "openai_chat"
base_url = "https://${FOUNDRY_NAME}.services.ai.azure.com/openai/v1"
api_key_env = "AZURE_AI_AUTH_TOKEN"
max_retries = 2

[targets.efficient]
id = "${EFFICIENT_MODEL_DEPLOYMENT}"
llm_client = "foundry"

[targets.capable]
id = "${CAPABLE_MODEL_DEPLOYMENT}"
llm_client = "foundry"

[routes.switchyard]
id = "switchyard"
type = "stage_router"
capable_target = "capable"
efficient_target = "efficient"
picker = "efficient_first"
confidence_threshold = 0.5
TOML

test -s "$SWITCHYARD_CONFIG" && echo "Switchyard configuration created."
```

Validate the configuration, start Switchyard on the container loopback
interface, and check its health endpoint. First confirm that the current shell
still has the managed-identity token:

```bash
test -n "${AZURE_AI_AUTH_TOKEN:-}" && echo "Foundry token is available."

switchyard-server --config "$SWITCHYARD_CONFIG" --dry-run

RUST_LOG="switchyard_server=info,libsy=info" \
  switchyard-server \
    --config "$SWITCHYARD_CONFIG" \
    --host 127.0.0.1 \
    --port 4000 \
    >/tmp/switchyard.log 2>&1 &
export SWITCHYARD_PID=$!

sleep 3

if kill -0 "$SWITCHYARD_PID" 2>/dev/null; then
  curl --fail --silent --show-error "http://127.0.0.1:4000/health"
else
  echo "Switchyard failed to start:"
  cat /tmp/switchyard.log
fi
```

Call the local Switchyard endpoint. The client-facing model is the route ID
`switchyard`; Switchyard replaces it with the selected Foundry deployment name.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  http://127.0.0.1:4000/v1/chat/completions \
  --header "Content-Type: application/json" \
  --data '{
    "model": "switchyard",
    "messages": [
      {
        "role": "user",
        "content": "Explain model routing in two sentences."
      }
    ]
  }'
```

Inspect Switchyard's routing and model-usage statistics:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:4000/v1/stats
```

The managed-identity token is short-lived. If Foundry returns `401`, stop
Switchyard, repeat the token-acquisition commands from the previous section,
and start Switchyard again so it reads the new `AZURE_AI_AUTH_TOKEN`.

Stop Switchyard when the exercise is complete:

```bash
kill "$SWITCHYARD_PID"
```

Delete the debug app when it is no longer needed:

```bash
az containerapp delete \
  --name "$DEBUG_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

## Optional: Enable Key Authentication

Microsoft Entra ID authentication is the secure default. To allow key-based
authentication for a lab scenario, deploy with `disableLocalAuth=false`:

```bash
az deployment group create \
  --name "deploy-switchyard-foundry" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters \
    location="$LOCATION" \
    namePrefix="switchyard" \
    disableLocalAuth=false
```

## Clean Up

Delete the resource group when the lab is complete to stop incurring charges:

```bash
az group delete \
  --name "$RESOURCE_GROUP" \
  --yes \
  --no-wait
```

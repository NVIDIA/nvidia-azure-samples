# Switchyard Hands-on Lab

This lab creates a Microsoft Foundry resource and project, an Azure Container
Apps environment, a user-assigned managed identity, and a Log Analytics
workspace. It then deploys one Switchyard Container App per participant from
the prebuilt image
`anslutskycontainerappacr.azurecr.io/switchyard-server:latest`.

Each participant supplies their own Foundry OpenAI-compatible base URL and two
model deployment names. Switchyard starts with that participant-specific
configuration mounted as a secret-backed file. Requests use the participant's
Microsoft Entra bearer token: Switchyard forwards the `Authorization` header
to Foundry, where the token is validated.

## Prerequisites

- An Azure subscription
- Azure CLI with Bicep support
- Permission to create resources and role assignments in a resource group
- Permission to pull from `anslutskycontainerappacr` and assign its `AcrPull`
  role to the lab managed identity
- Local `curl` for testing the public lab endpoint
- A [region that supports Microsoft Foundry](https://learn.microsoft.com/azure/ai-foundry/reference/region-support)
- Two deployed models that accept OpenAI-compatible Chat Completions requests

The Azure identity signed in to the CLI must be authorized to invoke the
participant's Foundry endpoint. The lab's managed identity is used to pull the
private ACR image; it is not used to authenticate inference requests in this
flow.

## Sign In to Azure

Run all commands from this sample directory:

```bash
cd inference/aca/switchyard_hol

az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
```

Register the required resource providers. Registration might require
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

Install or update the Container Apps CLI extension:

```bash
az extension add --name containerapp --upgrade
```

## Create a Resource Group

Choose a supported region and create a resource group:

```bash
export RESOURCE_GROUP="rg-switchyard-hol"
export LOCATION="<AZURE_REGION>"
export DEPLOYMENT_NAME="deploy-switchyard-foundry"

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"
```

## Deploy the Lab Infrastructure

Deploy the Foundry resource, its default project, the Container Apps
environment, managed identity, and Log Analytics workspace:

```bash
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP first.}"
: "${LOCATION:?Set LOCATION first.}"
: "${DEPLOYMENT_NAME:?Set DEPLOYMENT_NAME first.}"

az deployment group create \
  --name "$DEPLOYMENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters \
    location="$LOCATION" \
    namePrefix="switchyard"
```

The template appends a deterministic suffix to `namePrefix` so resource names
are repeatable within the resource group and the Foundry custom domain is
globally unique. The Container Apps environment uses the Consumption workload
profile and sends application logs to Log Analytics. Log Analytics retains
logs for 30 days by default; set `logRetentionDays` to a value from 30 through
730 to change it.

Retrieve the project endpoint after deployment:

```bash
az deployment group show \
  --name "$DEPLOYMENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.outputs.projectEndpoint.value \
  --output tsv
```

Open [Microsoft Foundry](https://ai.azure.com/) and select the project. Deploy
the two models used in this lab if they are not already available.

## Configure a Participant's Switchyard App

Repeat this section once per participant. Run the commands on the participant's
workstation, not in a Container App shell. Choose a short, unique, lowercase
identifier containing only letters, numbers, and hyphens. Because the
`switchyard-` prefix uses 11 characters and Container App names must be fewer
than 32 characters, the participant identifier can contain at most 20
characters. Container App names must be unique within the Container Apps
environment.

If this is a new terminal, set the deployment variables again. Empty variables
cause Azure CLI to construct an invalid deployment URL, so the checks stop the
script before that happens.

```bash
export RESOURCE_GROUP="rg-switchyard-hol"
export DEPLOYMENT_NAME="deploy-switchyard-foundry"
export PARTICIPANT_ID="<SHORT_UNIQUE_PARTICIPANT_ID>"
export SWITCHYARD_APP_NAME="switchyard-${PARTICIPANT_ID}"

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP first.}"
: "${DEPLOYMENT_NAME:?Set DEPLOYMENT_NAME first.}"
: "${PARTICIPANT_ID:?Set PARTICIPANT_ID first.}"
: "${SWITCHYARD_APP_NAME:?Could not construct the Container App name.}"

if [[ ! "$SWITCHYARD_APP_NAME" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || \
   [[ "$SWITCHYARD_APP_NAME" == *--* ]] || \
   [[ "$SWITCHYARD_APP_NAME" == *- ]]; then
  echo "Invalid Container App name; choose another PARTICIPANT_ID: $SWITCHYARD_APP_NAME" >&2
else
  echo "Container App name: $SWITCHYARD_APP_NAME"
fi
```

Retrieve the shared Container Apps environment and managed identity from the
Bicep deployment. Using `$DEPLOYMENT_NAME` avoids assuming a hard-coded
deployment name.

```bash
az deployment group show \
  --name "$DEPLOYMENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.provisioningState \
  --output tsv

export CONTAINER_APPS_ENVIRONMENT="$(
  az deployment group show \
    --name "$DEPLOYMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppsEnvironmentName.value \
    --output tsv
)"
export CONTAINER_APP_IDENTITY_ID="$(
  az deployment group show \
    --name "$DEPLOYMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppIdentityResourceId.value \
    --output tsv
)"
export CONTAINER_APP_IDENTITY_PRINCIPAL_ID="$(
  az deployment group show \
    --name "$DEPLOYMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.outputs.containerAppIdentityPrincipalId.value \
    --output tsv
)"

export SWITCHYARD_REGISTRY="anslutskycontainerappacr.azurecr.io"
export SWITCHYARD_IMAGE="${SWITCHYARD_REGISTRY}/switchyard-server:latest"
export SWITCHYARD_REGISTRY_ID="$(
  az acr show \
    --name "anslutskycontainerappacr" \
    --query id \
    --output tsv
)"

: "${CONTAINER_APPS_ENVIRONMENT:?Deployment output is empty.}"
: "${CONTAINER_APP_IDENTITY_ID:?Deployment output is empty.}"
: "${CONTAINER_APP_IDENTITY_PRINCIPAL_ID:?Deployment output is empty.}"
: "${SWITCHYARD_REGISTRY_ID:?Could not resolve the ACR resource ID.}"
```

Grant the managed identity permission to pull the private image. If an
instructor already created this assignment, Azure may report that it exists;
continue after confirming it is listed.

```bash
az role assignment create \
  --assignee-object-id "$CONTAINER_APP_IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPull" \
  --scope "$SWITCHYARD_REGISTRY_ID"

az role assignment list \
  --assignee-object-id "$CONTAINER_APP_IDENTITY_PRINCIPAL_ID" \
  --scope "$SWITCHYARD_REGISTRY_ID" \
  --query "[?roleDefinitionName=='AcrPull'].roleDefinitionName" \
  --output tsv
```

Role assignments can take several minutes to propagate.

Set the participant's Foundry OpenAI-compatible base URL and model deployment
names. Use deployment names, not model catalog names. Do not append
`/chat/completions` to the base URL.

```bash
export FOUNDRY_OPENAI_BASE_URL="https://<PARTICIPANT_FOUNDRY_NAME>.services.ai.azure.com/openai/v1"
export EFFICIENT_MODEL_DEPLOYMENT="<EFFICIENT_MODEL_DEPLOYMENT_NAME>"
export CAPABLE_MODEL_DEPLOYMENT="<CAPABLE_MODEL_DEPLOYMENT_NAME>"

: "${FOUNDRY_OPENAI_BASE_URL:?Set the participant Foundry base URL.}"
: "${EFFICIENT_MODEL_DEPLOYMENT:?Set the efficient deployment name.}"
: "${CAPABLE_MODEL_DEPLOYMENT:?Set the capable deployment name.}"
```

Generate a participant-specific Switchyard configuration locally. With
`forward_auth = true`, Switchyard forwards the caller's `Authorization` header
to Foundry instead of reading or storing an API key or token.

```bash
export SWITCHYARD_CONFIG_FILE="$(mktemp)"

cat > "$SWITCHYARD_CONFIG_FILE" <<TOML
schema_version = 1

[llm_clients.foundry]
format = "openai_chat"
base_url = "${FOUNDRY_OPENAI_BASE_URL}"
forward_auth = true
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

test -s "$SWITCHYARD_CONFIG_FILE" && \
  echo "Participant configuration created at $SWITCHYARD_CONFIG_FILE"
```

Create the participant's app. The `routes-config` secret is mounted as the
file `/etc/switchyard/routes-config`; it is not exposed as an environment
variable. Switchyard starts as the container's foreground process and listens
on the Container Apps ingress target port. Pass the Switchyard option and its
value as the single argument `--config=PATH`, attached directly to Azure CLI's
`--args` option. Separate `--config` and path tokens are not accepted by this
version of the Container Apps CLI extension. Switchyard defaults to host
`0.0.0.0` and port `4000`, so no explicit host or port arguments are needed.

Confirm the app name is still set before creating the resource. This is useful
when the configuration and deployment commands are run in different terminal
sessions.

```bash
: "${SWITCHYARD_APP_NAME:?Set SWITCHYARD_APP_NAME first.}"
echo "Creating Container App: $SWITCHYARD_APP_NAME"
```

```bash
az containerapp create \
  --name "$SWITCHYARD_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --environment "$CONTAINER_APPS_ENVIRONMENT" \
  --user-assigned "$CONTAINER_APP_IDENTITY_ID" \
  --registry-server "$SWITCHYARD_REGISTRY" \
  --registry-identity "$CONTAINER_APP_IDENTITY_ID" \
  --image "$SWITCHYARD_IMAGE" \
  --workload-profile-name "Consumption" \
  --secrets "routes-config=$(cat "$SWITCHYARD_CONFIG_FILE")" \
  --secret-volume-mount "/etc/switchyard" \
  --ingress external \
  --target-port 4000 \
  --transport http \
  --cpu 0.5 \
  --memory 1Gi \
  --min-replicas 1 \
  --max-replicas 1 \
  --command "switchyard-server" \
  --args=--config=/etc/switchyard/routes-config
```

After the app is ready, remove the temporary local copy of its configuration:

```bash
rm -f "$SWITCHYARD_CONFIG_FILE"
unset SWITCHYARD_CONFIG_FILE
```

## Test Switchyard from the Workstation

External ingress is enabled so the participant can call Switchyard with local
`curl`. Get the generated hostname and a short-lived Foundry access token for
the Azure identity currently signed in to the CLI:

```bash
export SWITCHYARD_FQDN="$(
  az containerapp show \
    --name "$SWITCHYARD_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn \
    --output tsv
)"
export AZURE_AI_AUTH_TOKEN="$(
  az account get-access-token \
    --resource "https://cognitiveservices.azure.com" \
    --query accessToken \
    --output tsv
)"

: "${SWITCHYARD_FQDN:?Container App FQDN is empty.}"
: "${AZURE_AI_AUTH_TOKEN:?Could not acquire a Foundry access token.}"
```

The signed-in identity must have data-plane access to the participant's
Foundry resource, such as the **Azure AI User** role. Because `forward_auth` is
enabled, the bearer token supplied below is passed through Switchyard and
validated by Foundry upstream.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "https://${SWITCHYARD_FQDN}/v1/chat/completions" \
  --header "Authorization: Bearer $AZURE_AI_AUTH_TOKEN" \
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

Optionally inspect health and routing statistics:

```bash
curl --fail --silent --show-error \
  "https://${SWITCHYARD_FQDN}/health"

curl --fail --silent --show-error \
  "https://${SWITCHYARD_FQDN}/v1/stats"
```

### External-ingress security notice

External ingress makes the Switchyard URL publicly reachable. The bearer token
on an inference request is still validated by Foundry, but Switchyard is not a
complete public authentication gateway, and operational endpoints such as
health or statistics might not require authentication. Use this setup only for
the hands-on lab, do not send production secrets or traffic through it, and do
not print or save the access token. For a durable deployment, use internal
ingress or a private network and place an appropriate authenticated gateway in
front of Switchyard.

Tokens returned by Azure CLI are short-lived. If Foundry returns `401`, acquire
a new token with the command above and retry the request; the Container App
does not need to restart.

Delete the participant app when the exercise is complete:

```bash
az containerapp delete \
  --name "$SWITCHYARD_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

## Optional: Enable Key Authentication

Microsoft Entra ID authentication is the secure default. To allow key-based
authentication for a separate lab scenario, deploy with
`disableLocalAuth=false`:

```bash
az deployment group create \
  --name "$DEPLOYMENT_NAME" \
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

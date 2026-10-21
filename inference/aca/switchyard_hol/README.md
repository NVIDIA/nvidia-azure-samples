# Switchyard Hands-on Lab

Skillable provides Microsoft Foundry model deployments, an Azure Container
Apps environment, Azure Container Registry (ACR), and Log Analytics. You will
build unchanged Switchyard source into ACR and deploy your own Container App.

You will copy an API key from the preinstalled Foundry resource and store it
as an Azure Container Apps secret. Switchyard reads that secret through an
environment variable and authenticates its own requests to Foundry. Your curl
and notebook clients do not need the Foundry key.

The app pulls its image using ACR credentials and exposes a public HTTPS
endpoint without client authentication or IP restrictions. This configuration
is only for the short-lived Skillable lab: anyone who knows the URL can invoke
models using the stored Foundry key and incur charges until the app is deleted.
No managed identity or model-inference role setup is required.

## Prerequisites

- Access to an Azure subscription containing the required resources
- A Bash terminal with Azure CLI
- Permission to create a Container App and read the lab resource keys
- Git and access to the public Switchyard repository
- An ACR with permission to run ACR Tasks builds and pull images
- Local `curl` for testing the public lab endpoint
- Python 3 with `pip` and virtual-environment support for the local Jupyter
  exercise
- Two deployed models that accept OpenAI-compatible Chat Completions requests

Skillable provides the active Azure CLI session used to discover resources,
run ACR Tasks builds, and create your app. These management operations still
need the lab's existing Azure permissions. Model inference uses the Foundry
API key instead of an Azure CLI access token.

Skillable must provide a Foundry account with key authentication enabled and
an ACR with its admin account enabled.

Install or update the Container Apps CLI extension:

```bash
az extension add --name containerapp --upgrade
```

## Identify the Azure Resources

This workflow expects the Foundry account, Container Apps environment, and ACR
to exist. The commands select the first item (`[0]`) returned by each Azure
resource list, which is assumed to be the correct resource in your Skillable
lab subscription. Run the command blocks as written. For deployment, you only
enter the Foundry API key and the efficient and capable model deployment
names; resource names, credentials, and endpoints are discovered
automatically, and app and image names are generated for you. For the local
Jupyter exercise, copy the discovered app hostname into PowerShell.

Select the first Foundry account and retrieve its resource group:

```bash
export FOUNDRY_NAME="$(az cognitiveservices account list --query '[0].name' --output tsv)"
export RESOURCE_GROUP="$(az cognitiveservices account list --query '[0].resourceGroup' --output tsv)"
: "${FOUNDRY_NAME:?Could not find a Foundry account.}"
: "${RESOURCE_GROUP:?Could not find the Foundry resource group.}"
echo "Foundry account: $FOUNDRY_NAME"
echo "Resource group: $RESOURCE_GROUP"
```

### Find Your Foundry API Key

Use the Foundry resource already installed in your Skillable environment.

1. Open **Azure Portal** from Skillable's Resources panel.
2. Open your lab resource group and select the Foundry account whose name
   was discovered in `FOUNDRY_NAME`, for example `myfoundry...`.
3. In the resource menu, open **Resource Management → Keys and Endpoint**
   (the label can also appear as **Keys and Endpoints**).
4. Copy **KEY 1** or **KEY 2**. Either key can authenticate requests.
   Do not regenerate keys during the lab because other exercises may use them.
5. If you are in the Foundry portal's resource overview, use **Endpoints and
   keys** to copy the account API key. Select the **Azure OpenAI** endpoint
   when checking the endpoint URL.

Use the key for the same account as your `FOUNDRY_NAME`. The base URL for this
lab is `https://<FOUNDRY_NAME>.openai.azure.com/openai/v1`.

Paste the key into this hidden Bash prompt, then press Enter. Your input will
not appear in the terminal or be saved as a command in shell history:

```bash
read -r -s -p "Paste your Foundry API key and press Enter: " AZURE_AI_API_KEY
```

Export the value for the deployment commands. It will be stored in an ACA
secret, not in the notebook or routing TOML. Confirm it is present without
displaying it:

```bash
export AZURE_AI_API_KEY
: "${AZURE_AI_API_KEY:?Foundry API key is empty.}"
echo "Foundry API key is set."
```

Do not print the key or paste it into notebook source, screenshots, or chat.
Do not enable shell tracing (`set -x`) or Azure CLI debug output while passing
secrets. Keep this deployment terminal open until app creation is complete;
you will clear the local key variable afterward.

### Find Your Container Apps Environment

Select the first Container Apps environment in the current subscription. The
environment might be in a different resource group from the Foundry account.

```bash
export CONTAINER_APPS_ENVIRONMENT="$(az containerapp env list --query '[0].name' --output tsv)"
export CONTAINER_APPS_ENVIRONMENT_ID="$(az containerapp env list --query '[0].id' --output tsv)"
: "${CONTAINER_APPS_ENVIRONMENT:?Could not find a Container Apps environment.}"
: "${CONTAINER_APPS_ENVIRONMENT_ID:?Could not resolve the Container Apps environment ID.}"
echo "$CONTAINER_APPS_ENVIRONMENT"
echo "$CONTAINER_APPS_ENVIRONMENT_ID"
```

## Configure Your Switchyard App

Run the following commands from your Bash terminal, not from inside a Container
App shell. Generate a timestamp and random suffix for your app and image tag;
you do not need to choose an identifier.

```bash
export PARTICIPANT_ID="$(date -u +%y%m%d%H%M%S)-${RANDOM}"
export SWITCHYARD_APP_NAME="switchyard-${PARTICIPANT_ID}"

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP first.}"
: "${CONTAINER_APPS_ENVIRONMENT_ID:?Set the Container Apps environment ID.}"
echo "Container App name: $SWITCHYARD_APP_NAME"
```

### Build Your Switchyard Image

Select the first ACR in the current subscription and resolve its login server:

```bash
export SWITCHYARD_REGISTRY_NAME="$(az acr list --query '[0].name' --output tsv)"
: "${SWITCHYARD_REGISTRY_NAME:?Could not find an ACR.}"
export SWITCHYARD_REGISTRY="$(az acr show --name "$SWITCHYARD_REGISTRY_NAME" --query loginServer --output tsv)"
: "${SWITCHYARD_REGISTRY:?Could not resolve the ACR login server.}"
echo "Registry: $SWITCHYARD_REGISTRY"
```

#### Read Your ACR Credentials

Container Apps uses the registry admin username and password to pull your
image. These are separate from the Foundry API key.

The lab provisioning enables the registry's admin account. Retrieve its
credentials automatically without printing the password:

```bash
export SWITCHYARD_REGISTRY_USERNAME="$(az acr credential show --name "$SWITCHYARD_REGISTRY_NAME" --query username --output tsv)"
export SWITCHYARD_REGISTRY_PASSWORD="$(az acr credential show --name "$SWITCHYARD_REGISTRY_NAME" --query "passwords[0].value" --output tsv)"
: "${SWITCHYARD_REGISTRY_USERNAME:?Could not read the ACR username.}"
: "${SWITCHYARD_REGISTRY_PASSWORD:?Could not read the ACR password.}"
echo "Registry credentials are loaded."
```

Do not print or save the password. ACR admin credentials cover registry image
access; `az acr build` uses your lab's Azure CLI permissions to start an Azure
build.

#### Clone the Pinned Switchyard Source

Use Switchyard `v0.3.0`, pinned to commit
`336196f6fbfc97ddc71c1700f6092e564e9f23c2`. Its unchanged Dockerfile has been
verified to build for Linux AMD64.

```bash
export SWITCHYARD_SOURCE_REF="336196f6fbfc97ddc71c1700f6092e564e9f23c2"
```

Clone the [Switchyard source](https://github.com/NVIDIA-NeMo/Switchyard) into a
temporary directory and check out that revision. These commands leave your
current working directory unchanged.

```bash
export SWITCHYARD_BUILD_DIR="$(mktemp -d)"
git clone --branch v0.3.0 --single-branch https://github.com/NVIDIA-NeMo/Switchyard.git "$SWITCHYARD_BUILD_DIR"
git -C "$SWITCHYARD_BUILD_DIR" checkout --detach "$SWITCHYARD_SOURCE_REF"
```

Tag the image with your automatically generated participant identifier:

```bash
: "${PARTICIPANT_ID:?Set PARTICIPANT_ID first.}"
: "${SWITCHYARD_REGISTRY:?Resolve the ACR login server first.}"
export SWITCHYARD_IMAGE_TAG="switchyard-server:lab-${PARTICIPANT_ID}-server-key-v1"
export SWITCHYARD_IMAGE="${SWITCHYARD_REGISTRY}/${SWITCHYARD_IMAGE_TAG}"
```

#### Build in Azure with ACR Tasks

ACR Tasks builds the upstream Dockerfile in Azure and pushes the resulting
Linux image directly to your lab registry. You do not need Docker or Rust
installed on your workstation. The build compiles Rust and can take several
minutes. See the
[ACR Tasks build guide](https://learn.microsoft.com/azure/container-registry/container-registry-quickstart-task-cli).

```bash
cd "$SWITCHYARD_BUILD_DIR"

az acr build \
  --registry "$SWITCHYARD_REGISTRY_NAME" \
  --image "$SWITCHYARD_IMAGE_TAG" \
  --platform linux/amd64 \
  --file Dockerfile .
```

#### Verify the Published Image

After the ACR build completes, confirm that your tag exists:

```bash
az acr repository show --name "$SWITCHYARD_REGISTRY_NAME" --username "$SWITCHYARD_REGISTRY_USERNAME" --password "$SWITCHYARD_REGISTRY_PASSWORD" --image "$SWITCHYARD_IMAGE_TAG" --query "{repository:name,digest:digest}" --output table
```

Optional validation:

```bash
echo "$SWITCHYARD_IMAGE"
```

### Configure Model Routing

List the model deployments available on the Foundry account:

```bash
az cognitiveservices account deployment list \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[].{deployment:name,model:properties.model.name,version:properties.model.version,state:properties.provisioningState}" \
  --output table
```

The OpenAI-compatible base URL is generated from the discovered account name:

```bash
export FOUNDRY_OPENAI_BASE_URL="https://${FOUNDRY_NAME}.openai.azure.com/openai/v1"
```

Enter the efficient and capable model deployment names from the `deployment`
column. These are the only resource selections you enter manually. Use
deployment names, not model catalog names.

```bash
export EFFICIENT_MODEL_DEPLOYMENT="<EFFICIENT_MODEL_DEPLOYMENT_NAME>"
```

```bash
export CAPABLE_MODEL_DEPLOYMENT="<CAPABLE_MODEL_DEPLOYMENT_NAME>"
```

Confirm the values are set:

```bash
: "${FOUNDRY_OPENAI_BASE_URL:?Set your Foundry base URL.}"
: "${EFFICIENT_MODEL_DEPLOYMENT:?Set the efficient deployment name.}"
: "${CAPABLE_MODEL_DEPLOYMENT:?Set the capable deployment name.}"
```

Generate your Switchyard configuration locally. `api_key_env` tells
Switchyard to read the Foundry key from its container environment.
`forward_auth = false` prevents incoming credentials and arbitrary application
headers from being forwarded. The key itself is not written into this TOML
file. These are supported settings in the unchanged
[pinned Switchyard configuration](https://github.com/NVIDIA-NeMo/Switchyard/blob/336196f6fbfc97ddc71c1700f6092e564e9f23c2/crates/switchyard-runner/src/config.rs#L654-L685).

```bash
export SWITCHYARD_CONFIG_FILE="$(mktemp)"

cat > "$SWITCHYARD_CONFIG_FILE" <<TOML
schema_version = 1

[llm_clients.foundry]
format = "openai_chat"
base_url = "${FOUNDRY_OPENAI_BASE_URL}"
forward_auth = false
api_key_env = "AZURE_AI_API_KEY"
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
  echo "Switchyard configuration created at $SWITCHYARD_CONFIG_FILE"
```

### Store the Foundry Key in ACA

Add two lab secrets: `routes-config` holds the key-free routing
TOML, and `foundry-api-key` holds your Foundry API key. The environment
variable `AZURE_AI_API_KEY=secretref:foundry-api-key` references the ACA secret;
do not substitute a literal key into `--env-vars`.

The CLI's `--secret-volume-mount` helper mounts all app secrets under
`/etc/switchyard`, including the routing file and Foundry key. Do not display
those files. Switchyard reads `/etc/switchyard/routes-config` and obtains its
credential from the secret-backed environment variable. See
[ACA secret handling](https://learn.microsoft.com/azure/container-apps/manage-secrets).

Create your app with public HTTPS ingress. No client key or IP allow rules
are required. Keep the attached `--args=--config=PATH` syntax shown in the
command.

```bash
(
  set -e
  : "${SWITCHYARD_APP_NAME:?Set SWITCHYARD_APP_NAME first.}"
  : "${AZURE_AI_API_KEY:?Enter your Foundry key using the hidden prompt.}"
  : "${SWITCHYARD_REGISTRY_PASSWORD:?Read your ACR credentials first.}"
  : "${SWITCHYARD_CONFIG_FILE:?Generate the routing configuration first.}"
  test -s "$SWITCHYARD_CONFIG_FILE"
  az containerapp create \
    --name "$SWITCHYARD_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$CONTAINER_APPS_ENVIRONMENT_ID" \
    --registry-server "$SWITCHYARD_REGISTRY" \
    --registry-username "$SWITCHYARD_REGISTRY_USERNAME" \
    --registry-password "$SWITCHYARD_REGISTRY_PASSWORD" \
    --image "$SWITCHYARD_IMAGE" \
    --workload-profile-name "Consumption" \
    --secrets "routes-config=$(cat "$SWITCHYARD_CONFIG_FILE")" "foundry-api-key=$AZURE_AI_API_KEY" \
    --env-vars "AZURE_AI_API_KEY=secretref:foundry-api-key" \
    --secret-volume-mount "/etc/switchyard" \
    --ingress external \
    --allow-insecure false \
    --target-port 4000 \
    --transport http \
    --cpu 0.5 \
    --memory 1Gi \
    --min-replicas 1 \
    --max-replicas 1 \
    --output none \
    --command "switchyard-server" \
    --args=--config=/etc/switchyard/routes-config
  echo "App created with the Foundry secret and public HTTPS ingress."
)
```

### Verify Your Container App

Confirm that `external` is `true`, `fqdn` contains your public hostname, and
`ipRestrictions` is empty (`[]` or `null`):

```bash
az containerapp show --name "$SWITCHYARD_APP_NAME" --resource-group "$RESOURCE_GROUP" --query "{external:properties.configuration.ingress.external,fqdn:properties.configuration.ingress.fqdn,ipRestrictions:properties.configuration.ingress.ipSecurityRestrictions}" --output json
```

Verify the secret reference without displaying the key:

```bash
az containerapp show --name "$SWITCHYARD_APP_NAME" --resource-group "$RESOURCE_GROUP" --query "properties.template.containers[0].env[?name=='AZURE_AI_API_KEY'].{name:name,secretRef:secretRef}" --output table
```

The reference should be `foundry-api-key`. After the app is ready, remove the
temporary key-free TOML and clear local credentials. Neither curl nor Jupyter
needs the Foundry key from this point onward:

```bash
rm -f "$SWITCHYARD_CONFIG_FILE"
unset SWITCHYARD_CONFIG_FILE
unset SWITCHYARD_REGISTRY_PASSWORD
unset AZURE_AI_API_KEY
```

## Test Your Switchyard App

Run this test in your Bash deployment terminal. Retrieve the app's public
hostname automatically:

```bash
export SWITCHYARD_FQDN="$(az containerapp show --name "$SWITCHYARD_APP_NAME" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn --output tsv)"
: "${SWITCHYARD_FQDN:?Container App FQDN is empty.}"
echo "$SWITCHYARD_FQDN"
```

Keep the printed hostname for the local PowerShell Jupyter setup below.

The request deliberately has **no Authorization header**. The lab endpoint
is public and unauthenticated; Switchyard supplies its own stored Foundry key
upstream.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "https://${SWITCHYARD_FQDN}/v1/chat/completions" \
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

## Run the LangChain Business Example

[`Switchyard_Client.ipynb`](Switchyard_Client.ipynb) builds a small customer
request triage application with LangChain. It sends an incoming customer
message to the `switchyard` route and produces a business brief with a
priority, owning team, issue summary, follow-up actions, and draft response.

The application uses LangChain's `ChatOpenAI` client because Switchyard exposes
an OpenAI-compatible Chat Completions endpoint. The client points at
`https://<SWITCHYARD_FQDN>/v1` and sends `model="switchyard"`. The SDK requires
an `api_key` argument, so the notebook uses the non-secret placeholder
`"not-used"`. This is not an access credential; the lab endpoint does not
authenticate clients. Switchyard ignores the client's placeholder and uses
its ACA secret for the selected efficient or capable Foundry deployment.

### Start Jupyter in Windows PowerShell

Open **Windows PowerShell** on your Skillable Windows VM or workstation with
Git, Python 3.10 or later (including the `py` launcher), and `curl.exe` installed.
Azure CLI and Azure sign-in are not needed on this Windows machine. The
deployment commands remain in Cloud Shell Bash; the commands in this section
run in local PowerShell.

Clone the [NVIDIA Azure samples repository](https://github.com/NVIDIA/nvidia-azure-samples)
from the folder where you want to keep your lab files:

```powershell
git clone https://github.com/NVIDIA/nvidia-azure-samples.git
Set-Location .\nvidia-azure-samples
Set-Location .\inference\aca\switchyard_hol
```

The clone commands switch to `switchyard_hol` and enter the lab directory.
Use it as the working directory for all remaining PowerShell commands.

In Cloud Shell, copy the hostname printed by `echo "$SWITCHYARD_FQDN"` in
**Test Your Switchyard App**. Paste that hostname between the quotes below in
your local PowerShell window. Copy only the hostname, without `https://` or
`/v1`. Jupyter and its Python kernel inherit this environment variable; do not
copy the Foundry API key.

```powershell
$env:SWITCHYARD_FQDN = "<SWITCHYARD_FQDN_FROM_CLOUD_SHELL>"
```

Optional validation:

```powershell
Write-Output $env:SWITCHYARD_FQDN
```

Create an isolated Python environment and install JupyterLab and its Python
kernel. The repository's `.gitignore` excludes `.venv`. These commands call
the environment's Python executable directly, so activation is not required.
See [Python virtual environments](https://docs.python.org/3/library/venv.html#how-venvs-work).

```powershell
py -3 -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install jupyterlab ipykernel
```

Register a named kernel inside `.venv` for this lab:

```powershell
.\.venv\Scripts\python.exe -m ipykernel install --sys-prefix --name switchyard-hol --display-name "Switchyard HoL (.venv)"
```

Start JupyterLab on the workstation loopback interface from the same PowerShell
window. Keep this window open while using the notebook.

```powershell
.\.venv\Scripts\python.exe -m jupyterlab --ip=127.0.0.1 --no-browser Switchyard_Client.ipynb
```

Open the `http://127.0.0.1:.../lab?token=...` URL printed by JupyterLab. The
token in this URL protects the local Jupyter service; do not share it. Keep the
service bound to `127.0.0.1` rather than exposing it on `0.0.0.0`.

### Run the notebook

Open `Switchyard_Client.ipynb`, select **Switchyard HoL (.venv)** as the kernel,
and run the cells from top to bottom:

1. **Install the integration** installs `langchain-openai` into the active
   notebook kernel with `%pip`.
2. **Connect to your Container App** reads the hostname from the
   `SWITCHYARD_FQDN` environment variable set in PowerShell. It does not read
   or request a Foundry key.
3. **Verify the endpoint with curl** sends a raw Chat Completions smoke test
   from Python using `curl.exe` on Windows. No Bash shell is required.
4. **Build a LangChain customer-request triage app** creates a prompt, a
   `ChatOpenAI` client, and the reusable `triage_customer_request` function.
5. **Run the business app** submits the included Contoso Retail example and
   renders the resulting business brief as Markdown.

Edit the values in the final cell to test another customer, account tier,
channel, or request. More complex requests may be routed differently depending
on the Switchyard configuration and the available Foundry deployments.

## Clean Up

Shut down the notebook kernel and stop Jupyter with `Ctrl-C` in its PowerShell
window. The virtual environment was used directly, so no deactivation step is
needed.

Delete the public app promptly when finished, even if Skillable will later
remove the environment. Deleting the app also removes its stored copy of the
Foundry key; clearing a terminal variable alone does not close the endpoint or
remove that copy from Azure.

In your Azure CLI deployment terminal, delete your app when the exercise is
complete:

```bash
az containerapp delete \
  --name "$SWITCHYARD_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

Delete only the Container App and other resources you created for this lab.
Retain shared or pre-existing resources unless you are responsible for their
lifecycle.

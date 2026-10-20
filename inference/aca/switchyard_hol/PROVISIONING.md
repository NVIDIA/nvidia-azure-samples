# Skillable Infrastructure Provisioning

Use `main.azuredeploy.json` as the single Switchyard infrastructure template.
It is compiled from `main.bicep`. Skillable provisions the Foundry account and
model deployments separately.

This template creates a Consumption Container Apps environment, a Log Analytics
workspace connected to that environment, and a Basic ACR with its admin account
enabled. Learners use the registry's username and password for Container App
image pulls and the existing Foundry account's API key for model inference.
During the lab, learners save that Foundry key as an ACA secret and reference
it through `AZURE_AI_API_KEY` in their Container App. The unchanged Switchyard
configuration uses `api_key_env` with `forward_auth = false`; clients do not
need the Foundry key.
The template creates no managed identities or role assignments and does not
create or update Foundry accounts, projects, model deployments, or Fireworks
endpoints. Learners create their own Container App during the lab.

## Deployment Parameters

No parameters are required.

All resources are created in the target deployment resource group.
`location` defaults to that resource group's location. `registryName` defaults
to `switchyard` plus a deterministic resource-group suffix. Optional parameters
also control `namePrefix`, `logRetentionDays`, and resource tags.

The deployment identity must be authorized to create these resources. No
permission to create role assignments is needed by this template. Provision
with incremental deployment mode when retaining an existing Foundry account.

`main.azuredeploy.json` includes the registry. The standalone
`acr.azuredeploy.json` is an alternative for environments that provide the
other resources separately.

## Prepare the Existing Foundry Resource

In Skillable's separate Foundry provisioning, ensure API-key authentication is
enabled: the account's `disableLocalAuth` property must be `false`. This
Switchyard template does not change that existing account. The participant
walkthrough shows learners where to copy a key and OpenAI-compatible endpoint
from the Azure portal's **Keys and Endpoint** page.

Skillable must give learners management access to discover the supplied
resources, view the Foundry key, read the ACR admin credentials, submit an
`az acr build`, and create their Container App. The ACR admin password grants
registry image access; it does not authorize submitting Azure Resource Manager
build requests. Learners use the active Azure CLI session provided by
Skillable for those management operations.

The template never accepts or outputs Foundry keys or registry passwords.
Learners load credentials into their terminal session when needed and should
not save credentials in source files or notebook outputs. Enabling the ACR
admin account is intended for this isolated lab; each environment should use
its own registry credentials.

The participant walkthrough intentionally creates a public HTTPS endpoint
without client authentication or IP restrictions for the short-lived Skillable
lab. Anyone who knows the endpoint URL can invoke models using the server-held
Foundry key and incur charges until the app is deleted. Limit this setup to
ephemeral lab resources and ensure teardown; it is not a production security
configuration.

## Generate and Validate the ARM File

Generate the ARM file used by Skillable from the Bicep source:

```bash
az bicep build --file main.bicep --outfile main.azuredeploy.json
```

Compilation checks the template locally. It does not verify Azure Policy,
regional availability, resource-provider registration, or the deployment
identity's permissions.

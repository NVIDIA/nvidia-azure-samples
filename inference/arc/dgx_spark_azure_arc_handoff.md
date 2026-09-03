# DGX Spark Azure Arc onboarding handoff

Purpose: onboard a DGX Spark device into an Azure subscription as an Azure
Arc-enabled server. This is Arc-only. It does not set up application
integrations, cloud gateways, Foundry agents, API Center entries, or media
storage.

## Azure target inputs

Confirm these values before onboarding:

| Setting | Value |
| --- | --- |
| Azure cloud | `AzureCloud` |
| Subscription name | `<subscription-name>` |
| Subscription ID | `<subscription-id>` |
| Tenant ID | `<tenant-id>` |
| Resource group | `<resource-group>` |
| Region | `<azure-region>` |
| Arc resource name | `<arc-resource-name>` |

Use a consistent set of inventory tags for each DGX Spark Arc resource:

```text
deviceType=DGX-Spark
accelerator=NVIDIA-GB10
architecture=arm64
environment=lab
managedBy=Azure-Arc
owner=<owner-or-team-alias>
workload=<short-purpose>
```

## Access checklist

The operator needs:

- An Azure account in tenant `<tenant-id>`.
- `Azure Connected Machine Onboarding` or `Contributor` on
  `<resource-group>` to connect the machine.
- `Reader` on the resource group if using portal-generated scripts or verifying
  the resource there.
- Local `sudo` access on the DGX Spark.
- Outbound HTTPS/TCP 443 from the DGX Spark to Azure Arc endpoints.

If they need to manage, delete, reconnect, or operate extensions after
onboarding, they may also need `Azure Connected Machine Resource Administrator`
or a broader team role on the resource group.

## Preflight from any shell with Azure CLI

```bash
export AZ_SUBSCRIPTION_ID="<subscription-id>"
export AZ_TENANT_ID="<tenant-id>"
export AZ_RESOURCE_GROUP="<resource-group>"

az login --tenant "$AZ_TENANT_ID"
az account set --subscription "$AZ_SUBSCRIPTION_ID"
az account show --query "{name:name,id:id,tenantId:tenantId,user:user.name}" -o table
az group show --name "$AZ_RESOURCE_GROUP" --query "{name:name,location:location}" -o table
```

Check the Arc providers. Registering providers may require Contributor, Owner,
or an equivalent governance role:

```bash
for ns in Microsoft.HybridCompute Microsoft.GuestConfiguration Microsoft.HybridConnectivity Microsoft.Compute; do
  az provider show --namespace "$ns" --query "{namespace:namespace,state:registrationState}" -o table
done
```

If any required provider is not registered:

```bash
for ns in Microsoft.HybridCompute Microsoft.GuestConfiguration Microsoft.HybridConnectivity Microsoft.Compute; do
  az provider register --namespace "$ns"
done
```

## Choose a unique Arc resource name

Pick a stable, human-readable name before connecting, for example:

```bash
export DGX_ARC_NAME="spark-<site-or-owner>-<number>"
```

The Arc resource name cannot be renamed in place. If the wrong name is used, the
usual fix is to disconnect and reconnect the agent, so it is worth choosing this
once with care. Avoid cloning an already connected Arc agent image because
duplicate Arc source IDs can make two machines behave like one resource.

## Install the Azure Connected Machine agent on the DGX Spark

These commands assume Ubuntu 24.04. If the DGX Spark image differs, use the
matching Microsoft package repository for that OS release.

```bash
cd /tmp
wget https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb
sudo dpkg -i packages-microsoft-prod.deb
sudo apt-get update
sudo apt-get install -y azcmagent
azcmagent version
```

## Connect the DGX Spark to Arc

Device-code login is the least brittle path for a shared/team subscription
because the command runs under `sudo` and does not depend on root seeing the
user's Azure CLI token cache.

```bash
export AZ_SUBSCRIPTION_ID="<subscription-id>"
export AZ_TENANT_ID="<tenant-id>"
export AZ_RESOURCE_GROUP="<resource-group>"
export AZ_LOCATION="<azure-region>"
export DGX_ARC_NAME="<arc-resource-name>"
export DGX_OWNER="<owner-or-team-alias>"
export DGX_WORKLOAD="<short-purpose>"

sudo azcmagent connect \
  --subscription-id "$AZ_SUBSCRIPTION_ID" \
  --tenant-id "$AZ_TENANT_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --location "$AZ_LOCATION" \
  --resource-name "$DGX_ARC_NAME" \
  --cloud AzureCloud \
  --use-device-code \
  --tags "deviceType=DGX-Spark,accelerator=NVIDIA-GB10,architecture=arm64,environment=lab,managedBy=Azure-Arc,owner=$DGX_OWNER,workload=$DGX_WORKLOAD"
```

If the installed Connected Machine agent is version 1.59 or newer and the Azure
CLI credentials are available to the command context, `--use-azcli` is also
supported. Device-code login is still the recommended first pass here.

## Verify on the DGX Spark

```bash
azcmagent show
systemctl status himdsd --no-pager
```

Expected result: `azcmagent show` reports `Connected` and includes a portal URL
for the Arc-enabled server resource.

After connection, every Arc-enabled server gets a system-assigned managed
identity. On Linux, local processes that need to use that identity must run as
root or as a user in the `himds` group. For Arc-only onboarding, you do not need
to add application users to `himds` unless a later workload needs managed
identity tokens from the local HIMDS endpoint.

## Verify from Azure

Run this from a logged-in Azure CLI session:

```bash
export AZ_SUBSCRIPTION_ID="<subscription-id>"
export AZ_RESOURCE_GROUP="<resource-group>"
export DGX_ARC_NAME="<arc-resource-name>"
export DGX_OWNER="<owner-or-team-alias>"
export DGX_WORKLOAD="<short-purpose>"

az connectedmachine show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$DGX_ARC_NAME" \
  --query "{name:name,status:status,location:location,identity:identity.type,tags:tags}" \
  -o json
```

List all team DGX Spark Arc machines:

```bash
az connectedmachine list \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --query "[?tags.deviceType=='DGX-Spark'].{name:name,status:status,owner:tags.owner,architecture:tags.architecture,workload:tags.workload}" \
  -o table
```

If tags need cleanup after the first connection:

```bash
ARC_ID=$(az connectedmachine show \
  --subscription "$AZ_SUBSCRIPTION_ID" \
  --resource-group "$AZ_RESOURCE_GROUP" \
  --name "$DGX_ARC_NAME" \
  --query id -o tsv)

az tag update \
  --resource-id "$ARC_ID" \
  --operation Merge \
  --tags deviceType=DGX-Spark accelerator=NVIDIA-GB10 architecture=arm64 environment=lab managedBy=Azure-Arc owner="$DGX_OWNER" workload="$DGX_WORKLOAD"
```

## Operating notes

- Keep DGX Spark Arc resources in a shared, well-known resource group unless
  there is a specific governance reason to split them. Shared resource-group
  RBAC keeps devices easier to discover and operate.
- The `deviceType=DGX-Spark` tag is the important inventory signal. Do not skip
  it, and keep the spelling consistent.
- Use a unique Arc resource name and never clone an already connected agent
  state onto a second device.
- Treat Arc as outbound-only device registration and identity. Do not open LAN
  dashboard ports to Azure just because the machine is Arc-enabled.
- Do not put credentials, service-principal secrets, private keys, or generated
  onboarding secrets in this repo. Device-code login avoids a persistent
  onboarding secret for a one-off device setup.
- Arc core support and Arc extension support are not identical on Arm64. The DGX
  Spark is tagged `architecture=arm64`; validate any later extension before
  assuming x86-64 parity.
- If onboarding fails after creating a partial Azure resource, inspect the
  Azure-side Arc machine and local `azcmagent show` state before retrying. Avoid
  disconnecting or deleting unrelated Arc resources while fixing a new device.

## Out of scope

Stop once the new DGX Spark appears as `Connected` in Azure Arc. Additional
application integrations, remote bridges, fleet gateways, Foundry connections,
API Center entries, and clip storage are separate workstreams and are
intentionally not part of this handoff.

## References

- Microsoft Learn: [Quickstart: Connect a Linux machine with Azure Arc-enabled servers](https://learn.microsoft.com/en-us/azure/azure-arc/servers/quick-onboard-linux)
- Microsoft Learn: [`azcmagent connect` CLI reference](https://learn.microsoft.com/en-us/azure/azure-arc/servers/azcmagent-connect)
- Microsoft Learn: [Connected Machine agent prerequisites](https://learn.microsoft.com/en-us/azure/azure-arc/servers/prerequisites)
- Microsoft Learn: [Connected Machine agent network requirements](https://learn.microsoft.com/en-us/azure/azure-arc/servers/network-requirements)
- Microsoft Learn: [Azure Arc-enabled server managed identity](https://learn.microsoft.com/en-us/azure/azure-arc/servers/managed-identity-authentication)

// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
// Complete Skillable lab provisioning. Paste this entire file into a Bicep
// Resource Template. It has no local modules or external file dependencies.
// Stock Ubuntu VMs install and validate host prerequisites during provisioning.
// AKS workloads, model broker, host tools and learner settings are automatic.
// Learners install the pinned agent and create their sandbox after connecting.
// Blank inference URL/key resolve the instance's existing Foundry deployment.

targetScope = 'resourceGroup'

@description('Azure resource region. Defaults to westus to match the reported VM-size availability list; verify eligibility and quota in the assigned Skillable subscription. An explicit profile mapping overrides this default.')
param location string = 'westus'

@description('Lowercase alphanumeric resource prefix, optionally containing hyphens.')
@minLength(2)
@maxLength(20)
param namePrefix string = 'ignite-agent'

@description('Nonsecret Skillable lab-instance identifier for resource tags.')
param labInstanceId string = 'development'

@description('Linux login name. Keep separate from the cloud portal username.')
param vmAdminUsername string = 'labuser'

@secure()
@minLength(12)
@maxLength(72)
@description('Per-instance Linux password supplied through Skillable credentials. Never output or commit it.')
param vmAdminPassword string

@description('CPU-only attendee VM size. The default provides 4 vCPUs and 16 GiB RAM.')
param vmSize string = 'Standard_D4s_v6'

@minValue(64)
@maxValue(512)
@description('Attendee VM OS disk capacity in GiB; allow space for Docker and the staged kit.')
param vmOsDiskSizeGiB int = 128

@description('Optional generalized x64 Linux image-version resource ID with Docker, Node.js, and the pinned kit. Empty uses stock Ubuntu with automatic tools and full lab setup.')
param vmImageResourceId string = ''

@description('Install and validate host tools through a blocking VM extension. Defaults to true for stock Ubuntu; a supplied rehearsed image uses its existing tools unless explicitly enabled. Lab applications/settings are controlled by deployLabSetup; agent installation by preinstallAgent.')
param installVmPrerequisites bool = empty(vmImageResourceId)

@description('Stock Ubuntu image version for infrastructure testing. Replace latest with a tested version for the event, or supply vmImageResourceId.')
param ubuntuImageVersion string = 'latest'

@maxLength(15)
@description('Optional single public IPv4 address allowed to SSH to the Ubuntu VM. Supply an address without CIDR; the rule always uses /32. Empty starts with SSH closed; the Cloud Shell helper sets the current session address after launch.')
param sshSourceIPv4 string = ''

@description('Optional SSH access from the AzureCloud service tag so Cloud Shell users can connect without a firewall helper. Includes public IPs used by other Azure customers, not only this lab or Cloud Shell. False preserves single-source or closed SSH access.')
param allowAzureCloudSsh bool = false

@description('CPU-only AKS system node size. No GPU node pool is created.')
param aksNodeVmSize string = 'Standard_D4s_v6'

// Follow the documented AKS system-pool minimum; validate in the event pool.
// https://learn.microsoft.com/azure/aks/use-system-pools
@minValue(2)
@maxValue(3)
@description('AKS system node count per lab instance. Defaults to the documented two-node system-pool minimum; benchmark the fixture before event delivery.')
param aksNodeCount int = 2

@description('Optional tested AKS version. Empty lets Azure select its regional default; pin after rehearsal.')
param kubernetesVersion string = ''

@description('Optional maintenance principal for direct AKS credential retrieval and cluster RBAC administration, never the attendee VM. Automatic lab setup uses its dedicated identity and does not require this input.')
param bootstrapPrincipalObjectId string = ''

@description('Optional explicit HTTPS OpenAI-compatible base URL. Leave this and inferenceApiKey empty to resolve the per-instance Foundry connection automatically. An explicit connection requires both values and inferenceModel; never use a project management URL.')
param inferenceEndpoint string = ''

@description('Exact model/deployment identifier. Empty selects NemotronLightning for automatic Foundry discovery; an explicit inference endpoint requires its actual model ID. This template does not create a model deployment.')
param inferenceModel string = ''

@description('Existing Foundry account created by the separate Fireworks template in this resource group. Defaults to myfoundry plus the mapped lab instance ID. Override if its naming changes; empty disables automatic discovery. Hide from students.')
param foundryAccountName string = 'myfoundry${labInstanceId}'

@description('Automatically deploy applications, the real inference broker and learner installation settings. Keep true for learner delivery. False is only for an infrastructure rehearsal and skips the applications and broker.')
param deployLabSetup bool = true

@description('Author-only option: install/onboard the agent and validate the sandbox during provisioning. Default false leaves installation, sandbox creation and MCP attachment to learners while preparing the full infrastructure. Hide from students.')
param preinstallAgent bool = false

@secure()
@description('Optional explicit upstream API key or broker token. Leave empty with inferenceEndpoint for automatic Foundry key retrieval by the setup identity. Explicit values must use a secure platform mapping, never source literals or outputs.')
param inferenceApiKey string = ''

@allowed(['Authorization', 'api-key'])
@description('Upstream credential header: Authorization uses Bearer; api-key is passed as an API key.')
param inferenceAuthHeader string = 'Authorization'

@description('Author-only revision: change to reset the application incident and learner sandbox, restage settings, and rerun the selected readiness checks.')
param setupRevision string = '1'

@description('Azure CLI deployment-script runtime. Version 2.65.0 includes jq required by the service wrapper. Verify availability in the event subscription pool.')
param deploymentScriptAzCliVersion string = '2.65.0'

@description('Additional tags. Core application and instance tags are always included.')
param tags object = {}

var suffix = uniqueString(resourceGroup().id)
var stem = '${toLower(namePrefix)}-${suffix}'
var vmName = '${stem}-vm'
var aksName = '${stem}-aks'
var nodeResourceGroupName = '${take(resourceGroup().name, 65)}-${suffix}-nodes'
var vmPrivateIp = '10.42.1.4'
var mcpPrivateIp = '10.42.4.10'
var labNamespace = 'ignite-lab'
var credentialVaultName = 'il${suffix}'
// Resolve inside the deployment script, after AKS provisioning. No ARM reference
// or listKeys call can race the separate Fireworks template at preflight time.
var resolveFoundryInference = !empty(foundryAccountName) && empty(inferenceEndpoint) && empty(inferenceApiKey)
var effectiveInferenceModel = resolveFoundryInference && empty(inferenceModel) ? 'NemotronLightning' : inferenceModel
var effectiveInferenceAuthHeader = resolveFoundryInference ? 'api-key' : inferenceAuthHeader
var resourceTags = union(tags, {
  application: 'ignite-nemoclaw-aks-lab'
  environment: 'lab'
  managedBy: 'bicep'
  labInstanceId: labInstanceId
})
var labContext = {
  schemaVersion: 3
  resourceGroupName: resourceGroup().name
  vmAdminUsername: vmAdminUsername
  aksClusterName: aksName
  aksNodeResourceGroupName: nodeResourceGroupName
  labNamespace: labNamespace
  vmPrivateIp: vmPrivateIp
  mcpPrivateIp: mcpPrivateIp
  mcpEndpoint: 'https://${mcpPrivateIp}:8443/mcp'
  deployLabSetup: deployLabSetup
  preinstallAgent: preinstallAgent
  setupRevision: setupRevision
  installVmPrerequisites: installVmPrerequisites
  credentialVaultName: credentialVaultName
  vmCredentialClientId: vmCredentialIdentity.properties.clientId
  resolveFoundryInference: resolveFoundryInference
  foundryAccountName: foundryAccountName
  upstreamInferenceEndpoint: inferenceEndpoint
  inferenceAuthHeader: effectiveInferenceAuthHeader
  inferenceEndpoint: deployLabSetup ? 'https://${mcpPrivateIp}:8000/v1' : inferenceEndpoint
  inferenceModel: effectiveInferenceModel
}

// Nonsecret context only. No model key, MCP token, or kubeconfig is placed in
// VM customData. The extension retrieves per-lab credentials with a limited identity.
// Plain Bicep multiline strings do not interpolate ${...}. Replace a marker
// explicitly, and encode the JSON to keep YAML parsing independent of values.
var cloudInit = replace('''
#cloud-config
write_files:
  - path: /etc/ignite-lab/context.json
    owner: root:root
    permissions: '0644'
    encoding: b64
    content: __IGNITE_LAB_CONTEXT_BASE64__
''', '__IGNITE_LAB_CONTEXT_BASE64__', base64(string(labContext)))

// Inline so Studio needs only this Bicep file. Node.js and kubelogin downloads
// are pinned with publisher-provided SHA-256 hashes, checked October 9, 2026.
// Docker and Azure CLI use signed vendor apt repositories; rehearse and freeze
// their package versions in the event image before publishing the final kit.
// AKS returns its full running version after provisioning; install that kubectl.
// The extension receives no passwords, model keys, tokens, or cluster identity.
// Generated by infra/build-template.py from the reviewed, nonsecret allowlist.
var setupBundle = '__IGNITE_SETUP_BUNDLE__'
var setupBundleSha256 = '__IGNITE_SETUP_SHA256__'
var unpackSetupScript = '''
import base64, gzip, hashlib, json, os
from pathlib import Path
payload = gzip.decompress(base64.b64decode(os.environ['SETUP_BUNDLE']))
if hashlib.sha256(payload).hexdigest() != os.environ['SETUP_SHA256']:
    raise SystemExit('Setup bundle integrity check failed')
root = Path('/opt/ignite-lab') if os.environ.get('VM_SETUP') else Path.cwd() / 'kit'
root.mkdir(parents=True, exist_ok=True)
for name, content in json.loads(payload).items():
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()):
        raise SystemExit('Invalid setup path')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(0o755 if name.endswith('.sh') else 0o644)
'''
var vmSetupScript = replace(replace(replace('''
#!/bin/sh
set -eu
export VM_SETUP=1
export SETUP_BUNDLE='__BUNDLE__'
export SETUP_SHA256='__HASH__'
if command -v cloud-init >/dev/null; then
  cloud_init_rc=0
  timeout 600 cloud-init status --wait || cloud_init_rc=$?
  [ "$cloud_init_rc" = 0 ] || [ "$cloud_init_rc" = 2 ] || exit 1
fi
mkdir -p /etc/ignite-lab
rm -f /etc/ignite-lab/ready.json
python3 - <<'UNPACK_SETUP'
__UNPACK__
UNPACK_SETUP
# cloud-init writes context before the extension; refresh it on author re-deploys.
printf '%s' '__CONTEXT__' | base64 --decode > /etc/ignite-lab/context.json
if python3 -c 'import json,sys; sys.exit(not json.load(open("/etc/ignite-lab/context.json"))["installVmPrerequisites"])'; then
  bash /opt/ignite-lab/infra/setup/host_prerequisites.sh
else
  for tool in python3 az kubectl kubelogin docker node npm; do command -v "$tool" >/dev/null; done
  docker info >/dev/null
fi
python3 /opt/ignite-lab/infra/setup/setup_vm.py
''', '__BUNDLE__', setupBundle), '__HASH__', setupBundleSha256), '__UNPACK__', unpackSetupScript)
var vmConfiguredScript = replace(vmSetupScript, '__CONTEXT__', base64(string(union(labContext, {
  kubernetesVersion: aks.properties.currentKubernetesVersion
}))))

resource outboundIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${stem}-egress-ip'
  location: location
  tags: resourceTags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-05-01' = {
  name: '${stem}-nat'
  location: location
  tags: resourceTags
  sku: { name: 'Standard' }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [{ id: outboundIp.id }]
  }
}

// Public ingress is for authenticated SSH from the selected /32, with optional
// broader AzureCloud access. AzureCloud is not specific to Cloud Shell or a tenant.
// The VM retains its private address for access to the private MCP service.
resource vmPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${stem}-vm-ip'
  location: location
  tags: resourceTags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource vmNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${stem}-vm-nsg'
  location: location
  tags: resourceTags
  properties: {
    securityRules: concat(!empty(sshSourceIPv4) ? [
      {
        name: 'AllowSshFromCloudShell'
        properties: {
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '${sshSourceIPv4}/32'
          sourcePortRange: '*'
          destinationAddressPrefix: vmPrivateIp
          destinationPortRange: '22'
        }
      }
    ] : [], allowAzureCloudSsh ? [
      {
        name: 'AllowSshFromAzureCloud'
        properties: {
          priority: 110
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'AzureCloud'
          sourcePortRange: '*'
          destinationAddressPrefix: vmPrivateIp
          destinationPortRange: '22'
        }
      }
    ] : [], [
      {
        name: 'DenyOtherInbound'
        properties: {
          priority: 4096
          protocol: '*'
          access: 'Deny'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ])
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${stem}-vnet'
  location: location
  tags: resourceTags
  properties: {
    addressSpace: { addressPrefixes: ['10.42.0.0/16'] }
    subnets: [
      {
        name: 'attendee'
        properties: {
          addressPrefix: '10.42.1.0/24'
          networkSecurityGroup: { id: vmNsg.id }
          natGateway: { id: natGateway.id }
        }
      }
      {
        name: 'aks-nodes'
        properties: {
          addressPrefix: '10.42.2.0/24'
          natGateway: { id: natGateway.id }
        }
      }
      {
        name: 'lab-services'
        properties: { addressPrefix: '10.42.4.0/24' }
      }
    ]
  }
}

resource aksIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${stem}-aks-identity'
  location: location
  tags: resourceTags
}

var networkContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions', '4d97b98b-1d4f-4787-a291-c67834d212e7'
)

// The AKS control plane manages its nodes, private DNS link, and internal LB.
// This identity is not attached to the attendee VM or the application pods.
resource aksNetworkRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vnet.id, aksIdentity.id, networkContributorRoleId)
  scope: vnet
  properties: {
    principalId: aksIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: networkContributorRoleId
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: aksName
  location: location
  tags: resourceTags
  sku: { name: 'Base', tier: 'Free' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${aksIdentity.id}': {} }
  }
  properties: {
    dnsPrefix: aksName
    nodeResourceGroup: nodeResourceGroupName
    enableRBAC: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      tenantID: tenant().tenantId
    }
    disableLocalAccounts: true
    apiServerAccessProfile: {
      enablePrivateCluster: true
      enablePrivateClusterPublicFQDN: false
      privateDNSZone: 'system'
    }
    agentPoolProfiles: [
      {
        name: 'systempool'
        mode: 'System'
        count: aksNodeCount
        vmSize: aksNodeVmSize
        osType: 'Linux'
        osSKU: 'Ubuntu'
        osDiskSizeGB: 128
        osDiskType: 'Managed'
        type: 'VirtualMachineScaleSets'
        maxPods: 30
        vnetSubnetID: '${vnet.id}/subnets/aks-nodes'
        enableAutoScaling: false
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      podCidr: '10.244.0.0/16'
      serviceCidr: '10.43.0.0/16'
      dnsServiceIP: '10.43.0.10'
      loadBalancerSku: 'standard'
      outboundType: 'userAssignedNATGateway'
    }
    ...(!empty(kubernetesVersion) ? { kubernetesVersion: kubernetesVersion } : {})
  }
  dependsOn: [aksNetworkRole]
}

var aksClusterUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions', '4abbcc35-e782-43d8-92c5-2d3f1bd2253f'
)
var aksRbacClusterAdminRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions', 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'
)

resource bootstrapClusterUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(bootstrapPrincipalObjectId)) {
  name: guid(aks.id, bootstrapPrincipalObjectId, aksClusterUserRoleId)
  scope: aks
  properties: {
    principalId: bootstrapPrincipalObjectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: aksClusterUserRoleId
  }
}

resource bootstrapClusterAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(bootstrapPrincipalObjectId)) {
  name: guid(aks.id, bootstrapPrincipalObjectId, aksRbacClusterAdminRoleId)
  scope: aks
  properties: {
    principalId: bootstrapPrincipalObjectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: aksRbacClusterAdminRoleId
  }
}

resource vmNic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${stem}-vm-nic'
  location: location
  tags: resourceTags
  properties: {
    ipConfigurations: [
      {
        name: 'private'
        properties: {
          privateIPAllocationMethod: 'Static'
          privateIPAddress: vmPrivateIp
          publicIPAddress: { id: vmPublicIp.id }
          subnet: { id: '${vnet.id}/subnets/attendee' }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: resourceTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${vmCredentialIdentity.id}': {} }
  }
  properties: {
    hardwareProfile: { vmSize: vmSize }
    storageProfile: {
      imageReference: !empty(vmImageResourceId) ? { id: vmImageResourceId } : {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: ubuntuImageVersion
      }
      osDisk: {
        name: '${vmName}-os'
        createOption: 'FromImage'
        diskSizeGB: vmOsDiskSizeGiB
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        deleteOption: 'Delete'
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: vmAdminUsername
      adminPassword: vmAdminPassword
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: false
        provisionVMAgent: true
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: vmNic.id, properties: { deleteOption: 'Delete' } }]
    }
    diagnosticsProfile: { bootDiagnostics: { enabled: true } }
  }
}

// Foreground template deployment waits for this extension to succeed. Its
// script setting executes a local inline payload; no hosted helper is needed.
resource vmPrerequisites 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = if (installVmPrerequisites || deployLabSetup) {
  parent: vm
  name: 'ignite-lab-setup'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.Extensions'
    type: 'CustomScript'
    typeHandlerVersion: '2.1'
    autoUpgradeMinorVersion: true
    forceUpdateTag: uniqueString(vmConfiguredScript, setupRevision)
    protectedSettings: {
      script: base64(vmConfiguredScript)
    }
  }
  dependsOn: [applicationSetup, vmCredentialRole]
}

// Dedicated provisioning identity can invoke a private AKS command through the
// management API. It is never attached to the attendee VM or application pods.
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${stem}-setup-identity'
  location: location
  tags: resourceTags
}
resource vmCredentialIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${stem}-credential-identity'
  location: location
  tags: resourceTags
}
// Cluster-scoped built-in role avoids subscription custom-role lifecycle state.
var setupClusterRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8')
resource setupClusterRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployLabSetup) {
  scope: aks
  name: guid(aks.id, setupIdentity.id, setupClusterRoleId)
  properties: {
    roleDefinitionId: setupClusterRoleId
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// AKS Contributor permits Run Command but has no Kubernetes data actions.
// Bootstrap must create the namespace, quota, Secrets and namespace RBAC.
// Grant this only to the provisioning identity on this lab's cluster.
resource setupClusterAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployLabSetup) {
  scope: aks
  name: guid(aks.id, setupIdentity.id, aksRbacClusterAdminRoleId)
  properties: {
    roleDefinitionId: aksRbacClusterAdminRoleId
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// The separate model template may run concurrently. Grant the setup identity
// read/list-keys access within this assigned resource group, whose scope exists
// before either template starts. Never grant this role to the learner VM identity.
// https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/ai-machine-learning#cognitive-services-user
var foundryUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
resource setupFoundryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployLabSetup && resolveFoundryInference) {
  name: guid(resourceGroup().id, setupIdentity.id, foundryUserRoleId)
  properties: {
    roleDefinitionId: foundryUserRoleId
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource credentialVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (deployLabSetup) {
  name: credentialVaultName
  location: location
  tags: resourceTags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    accessPolicies: []
  }
}
var secretsOfficerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
var secretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
resource setupVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployLabSetup) {
  scope: credentialVault
  name: guid(credentialVaultName, setupIdentity.id, secretsOfficerRoleId)
  properties: {
    roleDefinitionId: secretsOfficerRoleId
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// This vault contains ONLY the per-lab MCP/broker tokens and public CA. The
// shared provider key and server TLS private key are held only in AKS Secrets.
resource vmCredentialRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployLabSetup) {
  scope: credentialVault
  name: guid(credentialVaultName, vmCredentialIdentity.id, secretsUserRoleId)
  properties: {
    roleDefinitionId: secretsUserRoleId
    principalId: vmCredentialIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource applicationSetup 'Microsoft.Resources/deploymentScripts@2023-08-01' = if (deployLabSetup) {
  name: '${stem}-applications'
  location: location
  tags: resourceTags
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${setupIdentity.id}': {} }
  }
  properties: {
    azCliVersion: deploymentScriptAzCliVersion
    timeout: 'PT45M'
    cleanupPreference: 'Always'
    retentionInterval: 'PT1H'
    forceUpdateTag: uniqueString(setupBundleSha256, setupRevision, foundryAccountName, inferenceEndpoint, effectiveInferenceModel, effectiveInferenceAuthHeader)
    environmentVariables: concat([
      { name: 'SETUP_BUNDLE', value: setupBundle }
      { name: 'SETUP_SHA256', value: setupBundleSha256 }
      { name: 'LAB_CONTEXT', value: string(labContext) }
    ], empty(inferenceApiKey) ? [] : [{ name: 'INFERENCE_API_KEY', secureValue: inferenceApiKey }])
    scriptContent: 'set -eu\npython3 - <<\'UNPACK_SETUP\'\n${unpackSetupScript}\nUNPACK_SETUP\npython3 kit/infra/setup/deploy_cluster.py\n'
  }
  dependsOn: [setupClusterRole, setupClusterAdmin, setupVaultRole, setupFoundryRole]
}

// Nonsecret handoff outputs. Foreground deployment waits for application and VM setup.
output vmName string = vm.name
output vmResourceId string = vm.id
output vmAdminUsername string = vmAdminUsername
output vmPrivateIp string = vmPrivateIp
output vmPublicIp string = vmPublicIp.properties.ipAddress
output vmPublicIpResourceId string = vmPublicIp.id
output vmNetworkSecurityGroupName string = vmNsg.name
output vmPrerequisiteStatusPath string = installVmPrerequisites ? '/etc/ignite-lab/prerequisites.json' : ''
output sshCommand string = 'ssh ${vmAdminUsername}@${vmPublicIp.properties.ipAddress}'
output sshSourceIPv4 string = sshSourceIPv4
output virtualNetworkResourceId string = vnet.id
output aksClusterName string = aks.name
output aksClusterResourceId string = aks.id
output aksNodeResourceGroupName string = nodeResourceGroupName
output labNamespace string = labNamespace
output mcpServiceSubnetName string = 'lab-services'
output mcpPrivateIp string = mcpPrivateIp
output mcpEndpoint string = labContext.mcpEndpoint
output inferenceEndpoint string = labContext.inferenceEndpoint
output inferenceModel string = effectiveInferenceModel
output inferenceConnectionSource string = resolveFoundryInference ? 'foundry' : 'explicit'
output outboundPublicIp string = outboundIp.properties.ipAddress

output labReadinessPath string = deployLabSetup ? '/etc/ignite-lab/ready.json' : ''
output agentReadinessPath string = deployLabSetup ? '/home/${vmAdminUsername}/.config/ignite-lab/agent-ready.json' : ''
output expectedProvisioningStatus string = deployLabSetup ? (preinstallAgent ? 'ready' : 'infrastructure-ready') : 'infrastructure-only'
output learnerInstallCommand string = deployLabSetup ? 'source ~/.config/ignite-lab/environment.sh && bash /opt/ignite-lab/install-nemoclaw-pinned.sh --non-interactive --yes-i-accept-third-party-software' : ''
output learnerConfigureCommand string = deployLabSetup ? 'bash /opt/ignite-lab/infra/setup/setup_user.sh' : ''
output setupEnabled bool = deployLabSetup

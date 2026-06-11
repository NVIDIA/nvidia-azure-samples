// ============================================================================
// insurance-claims-ui — Streamlit on Azure Container Apps
//
// Subscription-scope deployment that:
//   1. Creates a new resource group for the UI
//   2. Provisions Log Analytics + Container Registry + Container Apps Env +
//      Container App with system-assigned managed identity
//   3. Cross-RG: grants the Container App's identity the "Foundry User" role
//      on the existing Foundry account (so DefaultAzureCredential inside the
//      pod can invoke the hosted insurance-claims-triage agent)
// ============================================================================

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment (used in resource naming)')
param environmentName string

@description('Primary location for all UI-tier resources')
param location string

@description('Name of the resource group to create for the UI tier')
param resourceGroupName string = 'rg-${environmentName}'

// --- Upstream wiring ---------------------------------------------------------

@description('VSS Agent base URL (workshop VSS deployment on AKS, plain HTTP)')
param vssBaseUrl string

@description('Foundry hosted-agent responses endpoint (Bearer auth)')
param foundryAgentEndpoint string

@description('Start the UI in OFFLINE mode (replay canned fixtures, no VSS / Foundry calls). Useful for a parallel ACA deployment that can serve demos while the AKS GPU cluster is shut down.')
param offlineMode bool = false

@description('Resource group of the existing Foundry account to grant the UI identity invoke rights on')
param foundryResourceGroupName string

@description('Name of the existing Foundry / Cognitive Services account')
param foundryAccountName string

// --- Container App tuning ----------------------------------------------------

@description('Container CPU (vCPU). Container Apps requires CPU/memory pairs from the published list.')
param containerCpu string = '0.5'

@description('Container memory (GiB). Must pair with cpu per Container Apps rules.')
param containerMemory string = '1.0Gi'

@description('Min replicas. 0 = scale-to-zero (cheap when idle, ~10s cold start).')
param minReplicas int = 0

@description('Max replicas (concurrency cap).')
param maxReplicas int = 2

// --- Naming -----------------------------------------------------------------

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
  workload: 'insurance-claims-ui'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'ui-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    abbrs: abbrs
    resourceToken: resourceToken
    vssBaseUrl: vssBaseUrl
    foundryAgentEndpoint: foundryAgentEndpoint
    offlineMode: offlineMode
    containerCpu: containerCpu
    containerMemory: containerMemory
    minReplicas: minReplicas
    maxReplicas: maxReplicas
  }
}

module foundryRbac 'modules/foundry-rbac.bicep' = {
  name: 'foundry-rbac'
  scope: resourceGroup(foundryResourceGroupName)
  params: {
    foundryAccountName: foundryAccountName
    principalId: resources.outputs.containerAppPrincipalId
  }
}

// --- Outputs (read by `azd env get-values` and the postdeploy step) ---------

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.containerRegistryEndpoint
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.containerRegistryName
output AZURE_CONTAINER_ENVIRONMENT_NAME string = resources.outputs.containerEnvironmentName
output SERVICE_UI_NAME string = resources.outputs.containerAppName
output SERVICE_UI_URI string = resources.outputs.containerAppUri

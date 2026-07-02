// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// =============================================================================
// AI-Q on Azure — workshop pre-deploy
// =============================================================================
//
// Provisions everything participants need EXCEPT:
//   - Container Apps (frontend + agent) — deployed live during the workshop
//   - Custom agent image in ACR — built post-deploy via `az acr build`
//   - Frontend image in ACR — imported post-deploy via `az acr import`
//
// All three model endpoints are provisioned here:
//   - Nemotron-3-Nano NIM (Azure ML online endpoint, A100 GPU)
//   - Llama-3.2-NV-embedqa-1b-v2 NIM (Azure ML online endpoint, A100 GPU)
//   - gpt-oss-120b (Azure AI Services account + GlobalStandard deployment,
//     per-token billed). First-time deployment in a subscription needs
//     one-time Marketplace terms acceptance — see the `aiServices` block.
//
// Each NIM consumes 24 vCPUs of Standard NCADSA100v4 family quota in the
// RG's region (96 needed total — 48 per participant if you're scaling).
//
// Usage:
//   az deployment group create \
//     --resource-group rg-aiq-workshop-xx \
//     --template-file main.bicep \
//     --parameters prefix=aiq
//
// A secure Postgres admin password is generated for each deployment and stored
// in the `postgres-password` Key Vault secret — no parameter needed.
// =============================================================================

targetScope = 'resourceGroup'

// -------------------- Parameters --------------------

@description('Location for all resources')
param location string = resourceGroup().location

@description('Resource name prefix')
param prefix string = 'aiq'

@description('Random suffix for globally unique resource names')
param suffix string = take(uniqueString(resourceGroup().id), 6)

@description('Postgres administrator username')
param pgAdminUser string = 'aiqadmin'

@description('Postgres administrator password. A new secure value meeting Azure complexity requirements is generated for each deployment when omitted.')
@secure()
param pgAdminPassword string = 'A${newGuid()}a!'

@description('Azure ML registry that hosts the NIM model assets. NVIDIA-published NIMs live in azureml-nvidia, not the public azureml registry.')
param nimRegistry string = 'azureml-nvidia'

@description('Azure ML registry model versions for the two NIMs. Bump these when NVIDIA publishes new NIM revisions in the Foundry catalog.')
param nemotronModelVersion string = '1'
param embedqaModelVersion string = '2'

@description('VM SKU for the NIM online endpoint deployments. Standard_NC24ads_A100_v4 is the smallest supported size per the Foundry catalog and is sufficient for workshop traffic.')
param nimInstanceType string = 'Standard_NC24ads_A100_v4'

@description('Azure AI Foundry Models version for gpt-oss-120b. Bump when Microsoft publishes a newer revision.')
param gptOssModelVersion string = '1'

@description('GlobalStandard throughput units for the gpt-oss-120b deployment. Capacity 1 = 1 RPM / 1K TPM, which is too low for the deep-research workflow (orchestrator + planner LLM both fire multiple completions per loop and the agent hammers 429s into a retry storm). 100 = 100 RPM / 100K TPM, plenty for a workshop demo. Tune up for production or down for cost.')
param gptOssCapacity int = 100

// -------------------- Naming --------------------

var laName = '${prefix}-law'
var appiName = '${prefix}-appi'
var uamiName = '${prefix}-uami'
var kvName = '${prefix}-kv-${suffix}'
var acrName = '${prefix}acr${suffix}'
var pgName = '${prefix}-pg-${suffix}'
var searchName = '${prefix}-search-${suffix}'
var acaEnvName = '${prefix}-env'
var foundryHubName = '${prefix}-foundry-hub'
var foundryProjectName = '${prefix}-foundry-project'
var foundryStorageName = 'foundry${suffix}'
var aiServicesName = '${prefix}-aiservices-${suffix}'

// Built-in role definition IDs
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultAdministrator: '00482a5a-887f-4fb3-b363-3b7fe8e74483'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  azureMLDataScientist: 'f6c7c914-8db3-469d-8ca1-694a8f32e121'  // for deployment-script traffic update
}

// -------------------- Observability --------------------

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: laName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: appiName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

// -------------------- Identity --------------------

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

// -------------------- Key Vault --------------------

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource kvUamiRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, uami.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.keyVaultSecretsUser
    )
    principalType: 'ServicePrincipal'
  }
}

// Secrets — depend on the admin role being in place
resource secPgPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'postgres-password'
  properties: { value: pgAdminPassword }
}

// -------------------- Container Registry --------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrUamiRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, roles.acrPull)
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.acrPull
    )
    principalType: 'ServicePrincipal'
  }
}

// -------------------- Postgres Flexible Server --------------------

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: pgName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    highAvailability: { mode: 'Disabled' }
  }
}

// Allow other Azure services through the firewall
resource pgFwAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: pg
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// PgBouncer not supported on Burstable tier — Container Apps connect directly on 5432.
// Re-enable (and switch tier to General Purpose) for production.

resource dbJobs 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: pg
  name: 'aiq_jobs'
}

resource dbCheckpoints 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: pg
  name: 'aiq_checkpoints'
}

// -------------------- Init: create the `job_info` table --------------------
// AI-Q's NAT job-store reads from `job_info` for deep-research orchestration
// but doesn't reliably auto-create the table on first boot. Initialize it
// here via an AzureCLI deployment script so participants don't have to run
// DDL manually. CREATE TABLE IF NOT EXISTS makes it idempotent across
// redeploys.

resource jobInfoInit 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${prefix}-init-job-info-${suffix}'
  location: location
  kind: 'AzureCLI'
  properties: {
    azCliVersion: '2.60.0'
    timeout: 'PT10M'
    retentionInterval: 'P1D'
    cleanupPreference: 'OnSuccess'
    forceUpdateTag: '1'
    storageAccountSettings: {
      storageAccountName: foundryStorage.name
      storageAccountKey: foundryStorage.listKeys().keys[0].value
    }
    environmentVariables: [
      { name: 'PGHOST', value: pg.properties.fullyQualifiedDomainName }
      { name: 'PGUSER', value: pgAdminUser }
      { name: 'PGPASSWORD', secureValue: pgAdminPassword }
      { name: 'PGDATABASE', value: dbJobs.name }
      { name: 'PGSSLMODE', value: 'require' }
    ]
    scriptContent: '''
      set -e
      apk add --no-cache postgresql-client
      psql -c "CREATE TABLE IF NOT EXISTS job_info (job_id VARCHAR PRIMARY KEY, status VARCHAR, config_file VARCHAR, error VARCHAR, output_path VARCHAR, created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE, expiry_seconds INTEGER, output VARCHAR, is_expired BOOLEAN);"
      echo "Ensured job_info table in $PGDATABASE on $PGHOST"
    '''
  }
  // dbJobs is implicit via `value: dbJobs.name`; only pgFwAzure needs to be explicit
  dependsOn: [
    pgFwAzure
  ]
}

// -------------------- Azure AI Search --------------------

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    semanticSearch: 'free'
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

resource searchDataRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, uami.id, roles.searchIndexDataContributor)
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.searchIndexDataContributor
    )
    principalType: 'ServicePrincipal'
  }
}

resource searchSvcRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, uami.id, roles.searchServiceContributor)
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.searchServiceContributor
    )
    principalType: 'ServicePrincipal'
  }
}

// -------------------- Container Apps environment --------------------

resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: acaEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// -------------------- Azure AI Foundry hub + project --------------------
//
// The hub + project anchor the NIM online-endpoint deployments below.

resource foundryStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: foundryStorageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
  }
}

resource foundryHub 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: foundryHubName
  location: location
  kind: 'Hub'
  identity: { type: 'SystemAssigned' }
  properties: {
    friendlyName: 'AI-Q workshop hub'
    publicNetworkAccess: 'Enabled'
    storageAccount: foundryStorage.id
    keyVault: kv.id
  }
}

resource foundryProject 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: foundryProjectName
  location: location
  kind: 'Project'
  identity: { type: 'SystemAssigned' }
  properties: {
    friendlyName: 'AI-Q workshop project'
    hubResourceId: foundryHub.id
  }
}

// -------------------- NIM model deployments --------------------
//
// Two managed online endpoints, one per NIM. Both deploy from the
// `azureml-nvidia` registry's curated NIM-microservice model assets (the
// public `azureml` registry does NOT contain these). Each consumes 24 vCPUs
// of Standard NCADSA100v4 family quota (1×A100 per endpoint).
//
// authMode: 'Key' so participants and the agent can use a static primary key
// fetched via listKeys() — the default (AMLToken) issues short-lived JWTs
// that don't fit the workshop's keyvaultref:// pattern.
//
// Deployments take ~10–15 minutes each to come up. Bicep total runtime grows
// accordingly; expect ~25–30 minutes for the full template on a fresh RG.

// --- Nemotron-3-Nano (chat / intent / summary) ---

resource nemoEndpoint 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01' = {
  parent: foundryProject
  name: 'nemotron-3-nano-nim'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    authMode: 'Key'
    publicNetworkAccess: 'Enabled'
  }
}

resource nemoDeployment 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments@2024-04-01' = {
  parent: nemoEndpoint
  name: 'default'
  location: location
  sku: { name: nimInstanceType, capacity: 1 }
  properties: {
    endpointComputeType: 'Managed'
    model: 'azureml://registries/${nimRegistry}/models/NVIDIA-Nemotron-3-Nano-NIM-microservice/versions/${nemotronModelVersion}'
    instanceType: nimInstanceType
    scaleSettings: { scaleType: 'Default' }
  }
}

resource secFoundryLlmKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'foundry-llm-key'
  properties: { value: nemoEndpoint.listKeys().primaryKey }
  dependsOn: [ nemoDeployment ]
}

// --- embedqa-1b-v2 (embeddings, 2048-dim) ---

resource embedEndpoint 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01' = {
  parent: foundryProject
  name: 'llama-3-2-nv-embedqa-1b-v2-nim'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    authMode: 'Key'
    publicNetworkAccess: 'Enabled'
  }
}

resource embedDeployment 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments@2024-04-01' = {
  parent: embedEndpoint
  name: 'default'
  location: location
  sku: { name: nimInstanceType, capacity: 1 }
  properties: {
    endpointComputeType: 'Managed'
    model: 'azureml://registries/${nimRegistry}/models/Llama-3.2-NV-embedqa-1b-v2-NIM-microservice/versions/${embedqaModelVersion}'
    instanceType: nimInstanceType
    scaleSettings: { scaleType: 'Default' }
  }
}

resource secFoundryEmbedKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'foundry-embed-key'
  properties: { value: embedEndpoint.listKeys().primaryKey }
  dependsOn: [ embedDeployment ]
}

// -------------------- Route traffic to the NIM deployments --------------------
//
// Newly created AML online deployments receive 0% traffic by default — you
// have to update the endpoint to point traffic at them. Bicep can't redeclare
// the endpoint to set traffic post-creation (same symbolic-name error), so
// we use a deployment script that runs after both deployments exist.

resource amlDeployerRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryProject
  name: guid(foundryProject.id, uami.id, roles.azureMLDataScientist)
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.azureMLDataScientist
    )
    principalType: 'ServicePrincipal'
  }
}

resource setNimTraffic 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${prefix}-set-nim-traffic-${suffix}'
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    azCliVersion: '2.60.0'
    timeout: 'PT10M'
    retentionInterval: 'P1D'
    cleanupPreference: 'OnSuccess'
    forceUpdateTag: '1'
    storageAccountSettings: {
      storageAccountName: foundryStorage.name
      storageAccountKey: foundryStorage.listKeys().keys[0].value
    }
    environmentVariables: [
      { name: 'RG', value: resourceGroup().name }
      { name: 'WORKSPACE', value: foundryProject.name }
      { name: 'NEMO_ENDPOINT', value: nemoEndpoint.name }
      { name: 'EMBED_ENDPOINT', value: embedEndpoint.name }
    ]
    scriptContent: '''
      set -e
      az extension add --name ml --yes --upgrade
      az ml online-endpoint update --resource-group "$RG" --workspace-name "$WORKSPACE" --name "$NEMO_ENDPOINT"  --traffic "default=100"
      az ml online-endpoint update --resource-group "$RG" --workspace-name "$WORKSPACE" --name "$EMBED_ENDPOINT" --traffic "default=100"
      echo "Routed 100% traffic to default deployments on both NIM endpoints."
    '''
  }
  dependsOn: [
    nemoDeployment
    embedDeployment
    amlDeployerRA
  ]
}

// -------------------- gpt-oss-120b serverless --------------------
//
// Standard / GlobalStandard model deployment on a dedicated Azure AI Services
// account. Per-token billing (no upfront vCPU allocation, no idle cost).
//
// First-time deployment in a subscription requires Marketplace terms
// acceptance for the OpenAI-OSS model. If Bicep errors with
// `MarketplaceTermsNotAccepted`, accept once via Azure portal (`Deploy` on
// the gpt-oss-120b model card) and re-run.

resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiServicesName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
  }
}

resource gptOssDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: 'gpt-oss-120b'
  sku: { name: 'GlobalStandard', capacity: gptOssCapacity }
  properties: {
    model: { format: 'OpenAI-OSS', name: 'gpt-oss-120b', version: gptOssModelVersion }
  }
}

resource secGptOssKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'gpt-oss-key'
  properties: { value: aiServices.listKeys().key1 }
  dependsOn: [ gptOssDeployment ]
}

// -------------------- Wire AI Services into the Foundry Project --------------------
//
// Without this connection, the gpt-oss-120b deployment doesn't show up
// alongside the NIMs in the Foundry portal's project view — it only
// appears under the standalone AI Services resource. The connection
// surfaces the AI Services account (and all its model deployments) inside
// `ai.azure.com` → project → Models + endpoints, so participants see one
// unified view of all three model endpoints.
//
// API-key auth for simplicity. For production, swap to AAD (set authType
// to 'AAD' and grant the project's MSI `Cognitive Services User` on the
// AI Services account).

resource aiServicesConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-04-01' = {
  parent: foundryProject
  name: aiServicesName
  properties: {
    category: 'AIServices'
    target: aiServices.properties.endpoint
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: aiServices.listKeys().key1
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: aiServices.id
    }
  }
  dependsOn: [ gptOssDeployment ]
}

// -------------------- Outputs --------------------

output rgName string = resourceGroup().name
output prefix string = prefix
output suffix string = suffix

// Identity
output uamiId string = uami.id
output uamiName string = uami.name
output uamiClientId string = uami.properties.clientId
output uamiPrincipalId string = uami.properties.principalId

// Key Vault
output kvName string = kv.name
output kvUri string = kv.properties.vaultUri

// ACR
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer

// Postgres
output pgName string = pg.name
output pgHost string = pg.properties.fullyQualifiedDomainName
output pgAdminUser string = pgAdminUser
output dbJobs string = dbJobs.name
output dbCheckpoints string = dbCheckpoints.name

// AI Search
output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'

// Container Apps
output acaEnvName string = acaEnv.name

// Foundry
output foundryHubName string = foundryHub.name
output foundryProjectName string = foundryProject.name

// NIM endpoints — scoring URIs end in /score; the agent uses the /v1 form
// (OpenAI-compatible). Strip /score and append /v1 client-side.
output nemotronScoringUri string = nemoEndpoint.properties.scoringUri
output embedqaScoringUri string = embedEndpoint.properties.scoringUri

// gpt-oss-120b — endpoint is the AI Services account's base + /models.
// The agent's config_web_azure.yml uses this URL verbatim; the OpenAI
// client appends /chat/completions when calling.
output gptOssEndpoint string = '${aiServices.properties.endpoint}models'
output aiServicesName string = aiServices.name

// Observability
output appiConnectionString string = appi.properties.ConnectionString
output lawName string = law.name
output lawCustomerId string = law.properties.customerId

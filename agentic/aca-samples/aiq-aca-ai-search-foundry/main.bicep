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
// The model tier follows AI-Q 2.2's upstream defaults:
//   - Nemotron 3.5 Lightning (Foundry managed compute) for intent and shallow research
//   - Nemotron 3 Embed 1B (Foundry managed compute) for native Azure AI Search vectors
//   - Nemotron 3 Ultra (Fireworks pay-per-token) for deep research
//
// Lightning and Embed use official models, runtime images, and A100 deployment
// templates from the Microsoft Foundry Hugging Face catalog.
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
@minLength(2)
param prefix string = 'aiq'

@description('Random suffix for globally unique resource names')
param suffix string = take(uniqueString(resourceGroup().id), 6)

@description('Postgres administrator username')
param pgAdminUser string = 'aiqadmin'

@description('Postgres administrator password. A new secure value meeting Azure complexity requirements is generated for each deployment when omitted.')
@secure()
param pgAdminPassword string = 'A${newGuid()}a!'

@description('Official Foundry catalog model and managed-compute template for Nemotron 3.5 Lightning.')
param lightningModel string = 'azureml://registries/azure-huggingface/models/nvidia--nvidia-nemotron-3.5-lightning-30b-a3b-nvfp4/versions/2'
param lightningDeploymentTemplate string = 'azureml://registries/azure-huggingface/deploymenttemplates/nvidia--nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4--256k-nvidia-a100/labels/latest'

@description('Official Foundry catalog model and managed-compute template for Nemotron 3 Embed 1B.')
param embeddingModel string = 'azureml://registries/azure-huggingface/models/nvidia--nemotron-3-embed-1b-bf16/versions/1'
param embeddingDeploymentTemplate string = 'azureml://registries/azure-huggingface/deploymenttemplates/nvidia--nemotron-3-embed-1b-bf16--nvidia-a100/labels/latest'

@description('Foundry Fireworks model version for Nemotron 3 Ultra.')
param ultraModelVersion string = '1'

@description('Azure region for Fireworks models. DataZoneStandard is currently available only in supported US regions.')
param ultraLocation string = 'eastus'

@description('DataZoneStandard capacity for the pay-per-token Nemotron 3 Ultra deployment. Deep research uses concurrent calls; 100 avoids throttling seen at 10.')
param ultraCapacity int = 100

// -------------------- Naming --------------------

var laName = '${prefix}-law'
var appiName = '${prefix}-appi'
var uamiName = '${prefix}-uami'
var kvName = '${prefix}-kv-${suffix}'
var acrName = '${prefix}acr${suffix}'
var pgName = '${prefix}-pg-${suffix}'
var searchName = '${prefix}-search-${suffix}'
var acaEnvName = '${prefix}-env'
var foundryProjectName = '${prefix}-project'
var foundryStorageName = 'foundry${suffix}'
var aiServicesName = '${prefix}-aiservices-${suffix}'
var dbInitSql = loadTextContent('init-db.sql')

// Built-in role definition IDs
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
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

// Secrets are written through the ARM control plane during deployment.
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

// -------------------- Initialize AI-Q 2.2 databases --------------------
// AI-Q needs job metadata, access, admission, event, summary, and LangGraph
// checkpoint tables before the backend starts. init-db.sql is idempotent.

resource databaseInit 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${prefix}-init-databases-${suffix}'
  location: location
  kind: 'AzureCLI'
  properties: {
    azCliVersion: '2.60.0'
    timeout: 'PT10M'
    retentionInterval: 'P1D'
    cleanupPreference: 'OnSuccess'
    forceUpdateTag: uniqueString(dbInitSql)
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
      { name: 'INIT_SQL_BASE64', value: base64(dbInitSql) }
    ]
    scriptContent: '''
      set -e
      apk add --no-cache postgresql-client
      echo "$INIT_SQL_BASE64" | base64 -d | psql --set ON_ERROR_STOP=1
      echo "Initialized AI-Q 2.2 database schemas on $PGHOST"
    '''
  }
  dependsOn: [
    dbCheckpoints
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

// -------------------- Deployment-script storage --------------------

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

// -------------------- Microsoft Foundry models --------------------
// DataZoneStandard is pay per token. Fireworks.EnableDeploy must be registered
// in the subscription, and Fireworks' non-Microsoft data terms apply.

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiServicesName
  location: ultraLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: aiServices
  name: foundryProjectName
  location: ultraLocation
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'AI-Q 2.2 workshop'
    description: 'AI-Q 2.2 with native Azure AI Search and managed NVIDIA models'
  }
}

resource lightningDeployment 'Microsoft.CognitiveServices/accounts/managedComputeDeployments@2026-05-15-preview' = {
  parent: aiServices
  name: 'nemotron-3-5-lightning'
  sku: { name: 'GlobalManagedCompute', capacity: 1 }
  properties: {
    model: lightningModel
    deploymentTemplate: lightningDeploymentTemplate
    acceleratorType: 'A100_80GB'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ foundryProject ]
}

resource embedDeployment 'Microsoft.CognitiveServices/accounts/managedComputeDeployments@2026-05-15-preview' = {
  parent: aiServices
  name: 'nemotron-3-embed-1b'
  sku: { name: 'GlobalManagedCompute', capacity: 1 }
  properties: {
    model: embeddingModel
    deploymentTemplate: embeddingDeploymentTemplate
    acceleratorType: 'A100_80GB'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ lightningDeployment ]
}

resource ultraDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: aiServices
  name: 'nemotron-3-ultra'
  sku: { name: 'DataZoneStandard', capacity: ultraCapacity }
  properties: {
    model: {
      format: 'Fireworks'
      name: 'FW-Nemotron-3-Ultra-NVFP4'
      version: ultraModelVersion
    }
  }
  dependsOn: [ embedDeployment ]
}

resource secFoundryKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'foundry-key'
  properties: { value: aiServices.listKeys().key1 }
  dependsOn: [
    lightningDeployment
    embedDeployment
    ultraDeployment
  ]
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
output foundryProjectName string = foundryProject.name
output foundryEndpoint string = 'https://${aiServicesName}.services.ai.azure.com/openai/v1/'
output aiServicesName string = aiServices.name

// Observability
output appiConnectionString string = appi.properties.ConnectionString
output lawName string = law.name
output lawCustomerId string = law.properties.customerId

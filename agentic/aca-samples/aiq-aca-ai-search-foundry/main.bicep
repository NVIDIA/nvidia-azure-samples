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
// Set deployModels=false for workshops using existing shared endpoints.
// In shared mode, no Foundry account, project, or models are created or updated.
//
// The model tier follows AI-Q 2.2.1's upstream defaults:
//   - Nemotron 3.5 Lightning (Fireworks pay-per-token) for intent and shallow research
//   - Nemotron 3 Embed 1B (NVIDIA hosted API) for native Azure AI Search vectors
//   - Nemotron 3 Ultra (Fireworks pay-per-token) for deep research
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

@description('Default location for supporting infrastructure; Search, PostgreSQL, and Foundry can override it.')
param location string = resourceGroup().location

@description('Azure AI Search region. Override if the default region cannot provision the selected tier.')
param searchLocation string = location

@description('PostgreSQL region. Override if the default region cannot provision the selected SKU.')
param postgresLocation string = location

@description('Deploy a Foundry account, project, and models. Set false to use existing shared model endpoints.')
param deployModels bool = true

@description('Lightning deployment name, also used by the application when calling shared models.')
param lightningDeploymentName string = 'nemotron-3-5-lightning'

@description('Ultra deployment name, also used by the application when calling shared models.')
param ultraDeploymentName string = 'nemotron-3-ultra'


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

@description('Foundry Fireworks model version for Nemotron 3.5 Lightning.')
param lightningModelVersion string = '1'

@description('Foundry Fireworks model version for Nemotron 3 Ultra.')
param ultraModelVersion string = '1'

@description('Azure region for the Foundry account. Both Fireworks models must support DataZoneStandard there.')
param foundryLocation string = 'eastus'

@description('DataZoneStandard capacity for pay-per-token Nemotron 3.5 Lightning.')
param lightningCapacity int = 100

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

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: pgName
  location: postgresLocation
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
resource pgFwAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = {
  parent: pg
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// PgBouncer not supported on Burstable tier — Container Apps connect directly on 5432.
// Re-enable (and switch tier to General Purpose) for production.

resource dbJobs 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: pg
  name: 'aiq_jobs'
}

resource dbCheckpoints 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: pg
  name: 'aiq_checkpoints'
}

// -------------------- Initialize AI-Q 2.2.1 databases --------------------
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
      echo "Initialized AI-Q 2.2.1 database schemas on $PGHOST"
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
  location: searchLocation
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

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (deployModels) {
  name: aiServicesName
  location: foundryLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = if (deployModels) {
  parent: aiServices
  name: foundryProjectName
  location: foundryLocation
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'AI-Q 2.2.1 workshop'
    description: 'AI-Q 2.2.1 with native Azure AI Search and NVIDIA models'
  }
}

resource lightningDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (deployModels) {
  parent: aiServices
  name: lightningDeploymentName
  sku: { name: 'DataZoneStandard', capacity: lightningCapacity }
  properties: {
    model: {
      format: 'Fireworks'
      name: 'FW-Nemotron-Lightning-3.5-30B-A3B'
      version: lightningModelVersion
    }
  }
  dependsOn: [ foundryProject ]
}

resource ultraDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (deployModels) {
  parent: aiServices
  name: ultraDeploymentName
  sku: { name: 'DataZoneStandard', capacity: ultraCapacity }
  properties: {
    model: {
      format: 'Fireworks'
      name: 'FW-Nemotron-3-Ultra-NVFP4'
      version: ultraModelVersion
    }
  }
  dependsOn: [ lightningDeployment ]
}

resource secFoundryKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (deployModels) {
  parent: kv
  name: 'foundry-key'
  properties: { value: aiServices!.listKeys().key1 }
  dependsOn: [
    lightningDeployment
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
output foundryProjectName string = deployModels ? foundryProject.name : ''
output foundryEndpoint string = deployModels ? 'https://${aiServicesName}.services.ai.azure.com/openai/v1/' : ''
output aiServicesName string = deployModels ? aiServices.name : ''
output lightningDeploymentName string = lightningDeploymentName
output ultraDeploymentName string = ultraDeploymentName

// Observability
output appiConnectionString string = appi.properties.ConnectionString
output lawName string = law.name
output lawCustomerId string = law.properties.customerId

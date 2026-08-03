// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region that supports the selected Container Apps GPU workload profile.')
param location string = resourceGroup().location

@description('Short prefix used to name the sample resources.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'nemotron-asr'

@description('NVIDIA NGC personal API key. The value is stored in Azure Key Vault.')
@secure()
param ngcApiKey string

@description('Container Apps serverless GPU workload profile type.')
param workloadProfileType string = 'Consumption-GPU-NC24-A100'

@description('Friendly name assigned to the GPU workload profile.')
param workloadProfileName string = 'gpu-a100'

@description('Tags applied to all supported resources.')
param tags object = {
  application: 'nemotron-asr-streaming'
  environment: 'demo'
  managedBy: 'bicep'
}

var suffix = uniqueString(resourceGroup().id)
var compactPrefix = replace(toLower(namePrefix), '-', '')
var acrName = take('${compactPrefix}${suffix}', 50)
var keyVaultName = take('${compactPrefix}-${suffix}', 24)
var environmentName = take('${namePrefix}-env-${suffix}', 60)
var identityName = take('${namePrefix}-identity-${suffix}', 128)
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

resource ngcSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ngc-api-key'
  properties: {
    value: ngcApiKey
  }
}

resource keyVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    workloadProfiles: [
      {
        name: workloadProfileName
        workloadProfileType: workloadProfileType
      }
    ]
  }
}

output acrName string = registry.name
output acrLoginServer string = registry.properties.loginServer
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output identityName string = identity.name
output identityResourceId string = identity.id
output ngcSecretUri string = '${keyVault.properties.vaultUri}secrets/${ngcSecret.name}'
output workloadProfileName string = workloadProfileName

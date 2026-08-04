// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region that supports the selected Container Apps GPU workload profile.')
param location string = resourceGroup().location

@description('Short prefix used to name the sample resources.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'freshroute-cuopt'

@description('Container Apps serverless GPU workload profile type.')
param workloadProfileType string = 'Consumption-GPU-NC24-A100'

@description('Friendly name assigned to the GPU workload profile.')
param workloadProfileName string = 'gpu-a100'

@description('Optional Microsoft Entra user object ID granted Azure Maps Data Reader for the local notebook.')
param notebookMapsPrincipalId string = ''

@description('Tags applied to all supported resources.')
param tags object = {
  application: 'freshroute-cuopt'
  environment: 'demo'
  managedBy: 'bicep'
}

var suffix = uniqueString(resourceGroup().id)
var compactPrefix = replace(toLower(namePrefix), '-', '')
var acrName = take('${compactPrefix}${suffix}', 50)
var mapsName = take('${namePrefix}-maps-${suffix}', 63)
var environmentName = take('${namePrefix}-env-${suffix}', 60)
var identityName = take('${namePrefix}-identity-${suffix}', 128)
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var mapsDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '423170ca-a8f6-4b0f-8487-9e4eb8f49bfa'
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

resource maps 'Microsoft.Maps/accounts@2023-06-01' = {
  name: mapsName
  location: location
  tags: tags
  kind: 'Gen2'
  sku: {
    name: 'G2'
  }
  properties: {
    disableLocalAuth: true
  }
}

resource mapsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(maps.id, identity.id, mapsDataReaderRoleId)
  scope: maps
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: mapsDataReaderRoleId
  }
}

resource notebookMapsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(notebookMapsPrincipalId)) {
  name: guid(maps.id, notebookMapsPrincipalId, mapsDataReaderRoleId)
  scope: maps
  properties: {
    principalId: notebookMapsPrincipalId
    principalType: 'User'
    roleDefinitionId: mapsDataReaderRoleId
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
output containerAppsEnvironmentDomain string = containerAppsEnvironment.properties.defaultDomain
output identityName string = identity.name
output identityResourceId string = identity.id
output identityClientId string = identity.properties.clientId
output mapsClientId string = maps.properties.uniqueId
output workloadProfileName string = workloadProfileName

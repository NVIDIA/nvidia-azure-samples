// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region where the lab resources are created.')
param location string = resourceGroup().location

@description('Short prefix used to name the lab resources.')
@minLength(2)
@maxLength(45)
param namePrefix string = 'switchyard'

@description('Disable key-based authentication and require Microsoft Entra ID authentication.')
param disableLocalAuth bool = true

@description('Number of days to retain Container Apps logs in Log Analytics.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

@description('Tags applied to all supported resources.')
param tags object = {
  application: 'switchyard-hol'
  environment: 'lab'
  managedBy: 'bicep'
}

var suffix = uniqueString(resourceGroup().id)
var foundryName = toLower('${namePrefix}-${suffix}')
var projectName = '${namePrefix}-project'
var containerAppsEnvironmentName = take(toLower('${namePrefix}-aca-env-${suffix}'), 60)
var logAnalyticsWorkspaceName = take(toLower('${namePrefix}-logs-${suffix}'), 63)
var containerAppIdentityName = take(toLower('${namePrefix}-aca-identity-${suffix}'), 128)
var foundryUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)

resource containerAppIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: containerAppIdentityName
  location: location
  tags: tags
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
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

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    disableLocalAuth: disableLocalAuth
    publicNetworkAccess: 'Enabled'
  }
}

resource containerAppFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, containerAppIdentity.id, foundryUserRoleId)
  scope: foundry
  properties: {
    principalId: containerAppIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: projectName
  parent: foundry
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Switchyard HOL'
    description: 'Microsoft Foundry project for the Switchyard hands-on lab.'
  }
}

output foundryName string = foundry.name
output foundryResourceId string = foundry.id
output projectName string = project.name
output projectResourceId string = project.id
output projectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.id
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name
output logAnalyticsWorkspaceResourceId string = logAnalyticsWorkspace.id
output logAnalyticsWorkspaceCustomerId string = logAnalyticsWorkspace.properties.customerId
output containerAppIdentityName string = containerAppIdentity.name
output containerAppIdentityResourceId string = containerAppIdentity.id
output containerAppIdentityClientId string = containerAppIdentity.properties.clientId
output containerAppIdentityPrincipalId string = containerAppIdentity.properties.principalId

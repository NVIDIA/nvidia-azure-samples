// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region where the lab resources are created.')
param location string = resourceGroup().location

@description('Short prefix used to name the lab resources.')
@minLength(2)
@maxLength(45)
param namePrefix string = 'switchyard'

@description('Globally unique ACR name containing only letters and numbers.')
@minLength(5)
@maxLength(50)
param registryName string = 'switchyard${uniqueString(resourceGroup().id)}'

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
var containerAppsEnvironmentName = take(toLower('${namePrefix}-aca-env-${suffix}'), 60)
var logAnalyticsWorkspaceName = take(toLower('${namePrefix}-logs-${suffix}'), 63)

resource registry 'Microsoft.ContainerRegistry/registries@2025-05-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
    roleAssignmentMode: 'LegacyRegistryPermissions'
    policies: {
      azureADAuthenticationAsArmPolicy: {
        status: 'enabled'
      }
    }
  }
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

output containerAppsEnvironmentName string = containerAppsEnvironment.name
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.id
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name
output logAnalyticsWorkspaceResourceId string = logAnalyticsWorkspace.id
output logAnalyticsWorkspaceCustomerId string = logAnalyticsWorkspace.properties.customerId
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output registryResourceId string = registry.id

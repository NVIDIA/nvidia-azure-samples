// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region containing the Container Apps environment.')
param location string = resourceGroup().location

@description('Name of the existing Container Apps environment.')
param containerAppsEnvironmentName string

@description('Default DNS domain of the existing Container Apps environment.')
param containerAppsEnvironmentDomain string

@description('Name of the user-assigned managed identity.')
param identityName string

@description('Client ID of the user-assigned managed identity.')
param identityClientId string

@description('Azure Maps account client ID used by the browser map control.')
param mapsClientId string

@description('ACR login server containing the imported cuOpt and web images.')
param acrLoginServer string

@description('Container Apps GPU workload profile name.')
param workloadProfileName string = 'gpu-a100'

@description('Image repository and immutable local tag for cuOpt in ACR.')
param cuoptTargetImage string = 'nvidia/cuopt:26.6.0-cuda12.9-py3.14'

@description('Image repository and tag for the FreshRoute web application in ACR.')
param webTargetImage string = 'freshroute/web:1.0.0'

@description('CIDR allowed to open the public FreshRoute web application.')
param allowedIpCidr string = ''

@description('Minimum warm cuOpt replicas. Zero minimizes idle GPU cost.')
@minValue(0)
@maxValue(1)
param cuoptMinReplicas int = 0

@description('Tags applied to both container apps.')
param tags object = {
  application: 'freshroute-cuopt'
  environment: 'demo'
  managedBy: 'bicep'
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: containerAppsEnvironmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

var cuoptAppName = 'cuopt-nim'
var webAppName = 'freshroute-web'
var cuoptBaseUrl = 'https://${cuoptAppName}.internal.${containerAppsEnvironmentDomain}'
var ipSecurityRestrictions = empty(allowedIpCidr) ? [] : [
  {
    name: 'AllowPresenter'
    description: 'Allow the explicitly configured presenter or customer network.'
    ipAddressRange: allowedIpCidr
    action: 'Allow'
  }
]

resource cuoptApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: cuoptAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: workloadProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: false
        targetPort: 5000
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: cuoptAppName
          image: '${acrLoginServer}/${cuoptTargetImage}'
          resources: {
            cpu: json('24.0')
            memory: '220Gi'
          }
          env: [
            {
              name: 'CUOPT_SERVER_PORT'
              value: '5000'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/cuopt/health'
                port: 5000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 15
              timeoutSeconds: 5
              failureThreshold: 40
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/cuopt/health'
                port: 5000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
      ]
      scale: {
        minReplicas: cuoptMinReplicas
        maxReplicas: 1
        cooldownPeriod: 300
        rules: [
          {
            name: 'http-one'
            http: {
              metadata: {
                concurrentRequests: '1'
              }
            }
          }
        ]
      }
    }
  }
}

resource webApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: webAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'http'
        ipSecurityRestrictions: ipSecurityRestrictions
      }
      registries: [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: webAppName
          image: '${acrLoginServer}/${webTargetImage}'
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'CUOPT_BASE_URL'
              value: cuoptBaseUrl
            }
            {
              name: 'CUOPT_WAKE_TIMEOUT_SECONDS'
              value: '720'
            }
            {
              name: 'AZURE_MAPS_CLIENT_ID'
              value: mapsClientId
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/health'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    cuoptApp
  ]
}

output webFqdn string = webApp.properties.configuration.ingress.fqdn
output cuoptFqdn string = cuoptApp.properties.configuration.ingress.fqdn

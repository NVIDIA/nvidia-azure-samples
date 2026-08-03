// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region containing the Container Apps environment.')
param location string = resourceGroup().location

@description('Name of the existing Container Apps environment.')
param containerAppsEnvironmentName string

@description('Name of the existing user-assigned managed identity.')
param identityName string

@description('ACR login server containing the imported NIM image.')
param acrLoginServer string

@description('Versionless Key Vault URI for the NGC API key secret.')
param ngcSecretUri string

@description('Container Apps GPU workload profile name.')
param workloadProfileName string = 'gpu-a100'

@description('Container app name.')
param containerAppName string = 'nemotron-asr-nim'

@description('Image repository and immutable local tag in ACR.')
param targetImage string = 'nvidia/nemotron-asr-streaming:amd64'

@description('Expose the gRPC endpoint outside the Container Apps environment.')
param externalIngress bool = false

@description('CIDR allowed to access external ingress. Leave empty only for internal ingress.')
param allowedIpCidr string = ''

@description('Minimum warm replicas. Zero minimizes idle GPU cost.')
@minValue(0)
@maxValue(1)
param minReplicas int = 1

@description('Tags applied to the container app.')
param tags object = {
  application: 'nemotron-asr-streaming'
  environment: 'demo'
  managedBy: 'bicep'
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: containerAppsEnvironmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

var ipSecurityRestrictions = externalIngress && !empty(allowedIpCidr) ? [
  {
    name: 'AllowClient'
    description: 'Allow the explicitly configured client network.'
    ipAddressRange: allowedIpCidr
    action: 'Allow'
  }
] : []

resource containerApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: containerAppName
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
        external: externalIngress
        allowInsecure: false
        targetPort: 50051
        transport: 'http2'
        ipSecurityRestrictions: ipSecurityRestrictions
      }
      registries: [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'ngc-api-key'
          keyVaultUrl: ngcSecretUri
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: containerAppName
          image: '${acrLoginServer}/${targetImage}'
          resources: {
            cpu: json('24.0')
            memory: '220Gi'
          }
          env: [
            {
              name: 'NGC_API_KEY'
              secretRef: 'ngc-api-key'
            }
            {
              name: 'NIM_HTTP_API_PORT'
              value: '9000'
            }
            {
              name: 'NIM_GRPC_API_PORT'
              value: '50051'
            }
            {
              name: 'NIM_TAGS_SELECTOR'
              value: 'name=nemotron-asr-streaming,type=multi'
            }
          ]
          probes: [
            {
              type: 'Startup'
              tcpSocket: {
                port: 50051
              }
              initialDelaySeconds: 60
              periodSeconds: 240
              timeoutSeconds: 10
              failureThreshold: 10
            }
            {
              type: 'Readiness'
              tcpSocket: {
                port: 50051
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    environment
    identity
  ]
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output ingressIsExternal bool = externalIngress

// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

targetScope = 'resourceGroup'

@description('Azure region for the AKS cluster and supporting resources.')
param location string = resourceGroup().location

@description('Short prefix used to name sample resources.')
@minLength(3)
@maxLength(24)
param namePrefix string = 'alpamayo-nim'

@description('AKS cluster name. Leave empty to generate a deterministic name from the resource group and prefix.')
param clusterName string = ''

@description('AKS system node pool name. AKS node pool names must be lowercase alphanumeric and at most 12 characters.')
@minLength(1)
@maxLength(12)
param systemNodePoolName string = 'system'

@description('AKS system node VM size.')
param systemNodeVmSize string = 'Standard_D8s_v5'

@description('Number of nodes in the AKS system node pool.')
@minValue(1)
@maxValue(10)
param systemNodeCount int = 1

@description('OS disk size in GiB for AKS system nodes.')
@minValue(64)
param systemOsDiskSizeGB int = 128

@description('GPU node pool name. AKS node pool names must be lowercase alphanumeric and at most 12 characters.')
@minLength(1)
@maxLength(12)
param gpuNodePoolName string = 'gpunim'

@description('GPU VM size. Default is a single 80 GB A100 node suitable for an initial Alpamayo 1.5 10B deployment; override for H100 or larger profiles.')
param gpuNodeVmSize string = 'Standard_NC24ads_A100_v4'

@description('Number of nodes in the GPU node pool.')
@minValue(1)
@maxValue(10)
param gpuNodeCount int = 1

@description('OS disk size in GiB for GPU nodes. NIM model caches and image layers can be large.')
@minValue(256)
param gpuOsDiskSizeGB int = 512

@description('Maximum pods per GPU node.')
@minValue(10)
@maxValue(250)
param gpuMaxPods int = 110

@description('Whether AKS should install GPU drivers on the GPU node pool.')
@allowed([
  'Install'
  'None'
])
param gpuDriver string = 'Install'

@description('NVIDIA GPU management mode. Managed asks AKS to install the device plugin and DCGM stack.')
@allowed([
  'Managed'
  'Unmanaged'
])
param gpuManagementMode string = 'Managed'

@description('MIG strategy for supported GPUs. Keep None for this single full-GPU NIM sample.')
@allowed([
  'None'
  'Single'
  'Mixed'
])
param gpuMigStrategy string = 'None'

@description('Taint the GPU node pool with sku=gpu:NoSchedule. Leave false for the AKS-managed GPU default.')
param taintGpuNodePool bool = false

@description('AKS node OS SKU.')
@allowed([
  'Ubuntu'
  'AzureLinux'
])
param osSku string = 'Ubuntu'

@description('Enable Azure Monitor Container Insights for the cluster.')
param enableAzureMonitor bool = true

@description('Tags applied to all supported resources.')
param tags object = {
  application: 'alpamayo-nim'
  environment: 'demo'
  managedBy: 'bicep'
}

var suffix = uniqueString(resourceGroup().id, namePrefix)
var resolvedClusterName = empty(clusterName) ? take('${namePrefix}-aks-${suffix}', 63) : clusterName
var dnsPrefix = take(replace(toLower('${resolvedClusterName}-${suffix}'), '_', '-'), 54)
var logAnalyticsWorkspaceName = take('${namePrefix}-logs-${suffix}', 63)
var nvidiaGpuProfile = gpuMigStrategy == 'None' ? {
  managementMode: gpuManagementMode
} : {
  managementMode: gpuManagementMode
  migStrategy: gpuMigStrategy
}
var gpuNodeTaints = taintGpuNodePool ? [
  'sku=gpu:NoSchedule'
] : []
var monitorAddonProfile = enableAzureMonitor ? {
  omsagent: {
    enabled: true
    config: {
      logAnalyticsWorkspaceResourceID: logAnalyticsWorkspace.id
    }
  }
} : {}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (enableAzureMonitor) {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource cluster 'Microsoft.ContainerService/managedClusters@2026-04-02-preview' = {
  name: resolvedClusterName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dnsPrefix: dnsPrefix
    enableRBAC: true
    agentPoolProfiles: [
      {
        name: systemNodePoolName
        count: systemNodeCount
        vmSize: systemNodeVmSize
        osDiskSizeGB: systemOsDiskSizeGB
        osDiskType: 'Managed'
        osSKU: osSku
        osType: 'Linux'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
        maxPods: 30
      }
    ]
    addonProfiles: monitorAddonProfile
    autoUpgradeProfile: {
      nodeOSUpgradeChannel: 'NodeImage'
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      podCidr: '10.244.0.0/16'
      serviceCidr: '10.0.0.0/16'
      dnsServiceIP: '10.0.0.10'
    }
  }
}

resource gpuNodePool 'Microsoft.ContainerService/managedClusters/agentPools@2026-04-02-preview' = {
  parent: cluster
  name: gpuNodePoolName
  properties: {
    count: gpuNodeCount
    vmSize: gpuNodeVmSize
    osDiskSizeGB: gpuOsDiskSizeGB
    osDiskType: 'Managed'
    osSKU: osSku
    osType: 'Linux'
    mode: 'User'
    type: 'VirtualMachineScaleSets'
    maxPods: gpuMaxPods
    nodeTaints: gpuNodeTaints
    nodeLabels: {
      workload: 'nim'
    }
    gpuProfile: {
      driver: gpuDriver
      nvidia: nvidiaGpuProfile
    }
  }
}

output clusterName string = cluster.name
output gpuNodePoolName string = gpuNodePool.name
output gpuNodeVmSize string = gpuNodeVmSize
output logAnalyticsWorkspaceName string = enableAzureMonitor ? logAnalyticsWorkspace.name : ''
output kubeconfigCommand string = 'az aks get-credentials --resource-group ${resourceGroup().name} --name ${cluster.name} --overwrite-existing'

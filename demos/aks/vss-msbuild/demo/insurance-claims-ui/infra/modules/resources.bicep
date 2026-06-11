// ============================================================================
// Resource-group-scoped module: creates the full UI tier in the new RG.
//
//   User-Assigned Managed Identity → AcrPull on registry (pre-creation) →
//   Container App with that MI pre-assigned (no bootstrap RBAC race) →
//   Log Analytics, Container Apps Environment, Container Registry
//
// NOTE: System-assigned MI on the Container App causes a chicken-and-egg
// problem here — the ACA platform tries to fetch a registry token via the
// MI as soon as the Container App is created, but the AcrPull role
// assignment is created _after_ the app (the principalId doesn't exist
// until then), and there's an extra RBAC propagation delay. Result: the
// initial revision fails with "ACR token exchange endpoint returned error
// status: 401" and times out after 20 min. User-assigned MI exists before
// the Container App, so AcrPull can be granted first.
// ============================================================================

param location string
param tags object
param abbrs object
param resourceToken string

param vssBaseUrl string
param foundryAgentEndpoint string

@description('Start the UI in OFFLINE mode (replay canned fixtures, no VSS / Foundry calls).')
param offlineMode bool = false

param containerCpu string
param containerMemory string
param minReplicas int
param maxReplicas int

// Built-in role: AcrPull — required so the Container App's managed identity
// can pull the streamlit image from the workload's own registry.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// --- User-Assigned Managed Identity ----------------------------------------

resource uaIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${abbrs.managedIdentityUserAssignedIdentities}ui-${resourceToken}'
  location: location
  tags: tags
}

// --- Log Analytics ---------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// --- Container Registry ----------------------------------------------------

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: '${abbrs.containerRegistryRegistries}${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// --- RBAC: grant the user-assigned MI AcrPull BEFORE the Container App ----

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: containerRegistry
  name: guid(containerRegistry.id, uaIdentity.id, acrPullRoleId)
  properties: {
    principalId: uaIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// --- Container Apps Environment --------------------------------------------

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${abbrs.appManagedEnvironments}${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
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

// --- Container App (Streamlit) ---------------------------------------------

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-ui-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'ui' })
  // Explicit dependsOn so ARM doesn't try to create the Container App until
  // the AcrPull role assignment is in place. (Bicep would normally infer
  // this via the principalId reference below, but the assignment is on a
  // sibling resource so we make the ordering explicit.)
  dependsOn: [
    acrPullAssignment
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uaIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        // targetPort must match the listening port baked into the image.
        // Streamlit's Dockerfile CMD passes `--server.port=80`, and the
        // placeholder image below also listens on 80 — so the platform's
        // startup probes succeed on first revision.
        external: true
        targetPort: 80
        transport: 'auto'
        allowInsecure: false
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          // Use the user-assigned MI's resourceId (not the literal string
          // 'system') for registry auth.
          identity: uaIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          // azd replaces this placeholder with the built image reference on
          // first `azd deploy`. The placeholder is the canonical azd
          // Container Apps hello image which listens on port 80 — matches
          // the ingress targetPort above so the first revision comes up
          // healthy before the real image is pushed.
          name: 'ui'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          env: [
            { name: 'VSS_BASE_URL', value: vssBaseUrl }
            { name: 'FOUNDRY_AGENT_ENDPOINT', value: foundryAgentEndpoint }
            { name: 'STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION', value: 'false' }
            // Boot directly into offline mode when true. Lets a parallel ACA
            // deployment serve demos while the AKS GPU cluster (cosmos VLM)
            // is shut down to save cost. Reps can still toggle via Settings.
            { name: 'OFFLINE_MODE', value: offlineMode ? 'true' : 'false' }
            // DefaultAzureCredential needs to know WHICH managed identity to
            // request a token for when there's no system-assigned MI. With a
            // user-assigned MI only, the SDK can't auto-discover — setting
            // AZURE_CLIENT_ID makes ManagedIdentityCredential request tokens
            // for this specific MI. Without it, the in-pod call to
            // DefaultAzureCredential().get_token(...) raises
            // ClientAuthenticationError with "Unable to load the proper
            // Managed Identity".
            { name: 'AZURE_CLIENT_ID', value: uaIdentity.properties.clientId }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

// --- Outputs ---------------------------------------------------------------

output containerRegistryEndpoint string = containerRegistry.properties.loginServer
output containerRegistryName string = containerRegistry.name
output containerEnvironmentName string = containerAppEnv.name
output containerAppName string = containerApp.name
output containerAppUri string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
// Principal ID of the UI's user-assigned MI — passed up to main.bicep and
// then into the foundry-rbac module for the cross-RG Foundry User grant.
output containerAppPrincipalId string = uaIdentity.properties.principalId

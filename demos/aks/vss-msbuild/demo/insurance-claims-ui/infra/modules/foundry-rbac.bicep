// ============================================================================
// Cross-RG role assignment.
//
// Deployed to the EXISTING Foundry resource group (rg-insurance-claims-foundry-dev
// in this project), grants the UI Container App's system-assigned identity the
// "Foundry User" role on the Foundry account so the in-pod DefaultAzureCredential
// can invoke the hosted insurance-claims-triage agent.
//
// Role: Foundry User (formerly Azure AI User) — least-privilege role that
// covers all CognitiveServices data actions including OpenAI Responses API.
// Built-in role GUID: 53ca6127-db72-4b80-b1b0-d745d6d5456d
// ============================================================================

@description('Name of the existing Foundry / Cognitive Services account')
param foundryAccountName string

@description('Principal ID of the UI Container App managed identity (system-assigned)')
param principalId string

@description('Built-in role: Foundry User / Azure AI User. Default is the well-known role GUID.')
param foundryUserRoleId string = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, principalId, foundryUserRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryUserRoleId
    )
  }
}

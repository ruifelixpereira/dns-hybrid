targetScope = 'subscription'

@description('Central managing tenant ID.')
param managedByTenantId string

@description('Deterministic GUID for this resource-group delegation.')
param registrationName string

@description('Unique, human-readable offer name.')
param offerName string

@description('Existing resolver resource group to delegate; the subscription is not delegated.')
param resolverResourceGroup string

type Authorization = {
  principalId: string
  principalIdDisplayName: string
  roleDefinitionId: string
}

@description('Central user-assigned managed identity with Network Contributor.')
@minLength(1)
@maxLength(1)
param authorizations Authorization[]

resource definition 'Microsoft.ManagedServices/registrationDefinitions@2022-10-01' = {
  name: registrationName
  properties: {
    registrationDefinitionName: offerName
    description: 'Lab 03: Network Contributor over this entire resolver resource group.'
    managedByTenantId: managedByTenantId
    authorizations: authorizations
  }
}

module assignment './lighthouse-assignment.bicep' = {
  scope: resourceGroup(resolverResourceGroup)
  name: '${offerName}-assignment'
  params: {
    registrationName: registrationName
    registrationDefinitionId: definition.id
  }
}

output registrationAssignmentId string = assignment.outputs.registrationAssignmentId

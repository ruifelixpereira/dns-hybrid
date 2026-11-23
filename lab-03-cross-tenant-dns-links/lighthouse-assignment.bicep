targetScope = 'resourceGroup'

@description('Deterministic GUID for this resource-group delegation.')
param registrationName string

@description('Fully qualified subscription-scoped Lighthouse registration definition ID.')
param registrationDefinitionId string

resource assignment 'Microsoft.ManagedServices/registrationAssignments@2022-10-01' = {
  name: registrationName
  properties: {
    registrationDefinitionId: registrationDefinitionId
  }
}

output registrationAssignmentId string = assignment.id

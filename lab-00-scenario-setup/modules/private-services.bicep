param location string
param prefix string
param endpointSubnetId string
param blobEndpointIp string
param vaultEndpointIp string
param blobZoneId string
param vaultZoneId string
param tags object

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, prefix)

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: 'st${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    publicNetworkAccess: 'Disabled'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: 'kv-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

resource blobEndpoint 'Microsoft.Network/privateEndpoints@2025-01-01' = {
  name: '${prefix}-blob'
  location: location
  tags: tags
  properties: {
    subnet: { id: endpointSubnetId }
    ipConfigurations: [
      {
        name: 'blob'
        properties: {
          groupId: 'blob'
          memberName: 'blob'
          privateIPAddress: blobEndpointIp
        }
      }
    ]
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource vaultEndpoint 'Microsoft.Network/privateEndpoints@2025-01-01' = {
  name: '${prefix}-vault'
  location: location
  tags: tags
  properties: {
    subnet: { id: endpointSubnetId }
    ipConfigurations: [
      {
        name: 'vault'
        properties: {
          groupId: 'vault'
          memberName: 'default'
          privateIPAddress: vaultEndpointIp
        }
      }
    ]
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: vault.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

resource blobZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-01-01' = {
  parent: blobEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'blob', properties: { privateDnsZoneId: blobZoneId } }
    ]
  }
}

resource vaultZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-01-01' = {
  parent: vaultEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'vault', properties: { privateDnsZoneId: vaultZoneId } }
    ]
  }
}

output services object = {
  storageId: storage.id
  vaultId: vault.id
  blobFqdn: '${storage.name}.blob.${environment().suffixes.storage}'
  vaultFqdn: '${vault.name}${environment().suffixes.keyvaultDns}'
  blobEndpointId: blobEndpoint.id
  vaultEndpointId: vaultEndpoint.id
  blobIp: blobEndpointIp
  vaultIp: vaultEndpointIp
}


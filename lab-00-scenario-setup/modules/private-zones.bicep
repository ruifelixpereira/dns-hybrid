param vnetIds string[]
param standaloneVnetId string
param tags object

resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource vaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource blobLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (id, i) in vnetIds: {
  parent: blobZone
  name: 'vnet-${i}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: id }
  }
}]

resource vaultLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (id, i) in vnetIds: {
  parent: vaultZone
  name: 'vnet-${i}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: id }
  }
}]

resource registryZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: tags
}

resource registryLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: registryZone
  name: 'standalone'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: standaloneVnetId }
  }
}

output registryZoneId string = registryZone.id
output blobZoneId string = blobZone.id
output vaultZoneId string = vaultZone.id


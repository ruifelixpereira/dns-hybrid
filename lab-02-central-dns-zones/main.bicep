targetScope = 'resourceGroup'

@description('Private DNS zone names selected from create operations in the consolidated plan.')
@maxLength(800)
param zoneNames string[]

@description('Resource ID of the central resolver virtual network.')
param resolverVnetId string

type VnetLink = {
  zoneName: string
  name: string
}

@description('Missing resolver VNet links for zones in this resource group.')
@maxLength(800)
param vnetLinks VnetLink[]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zoneName in zoneNames: {
  name: zoneName
  location: 'global'
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for link in vnetLinks: {
  name: '${link.zoneName}/${link.name}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: resolverVnetId
    }
  }
  dependsOn: [
    zones
  ]
}]

output zoneIds array = [for (zoneName, i) in zoneNames: zones[i].id]

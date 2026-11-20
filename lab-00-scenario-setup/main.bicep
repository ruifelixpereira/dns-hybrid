targetScope = 'subscription'

@description('Use a region that supports DNS Private Resolver and the selected VM image/SKU.')
param location string = 'swedencentral'

@minLength(3)
@maxLength(12)
@description('Lowercase letters, digits and hyphens; start with a letter.')
param prefix string

@description('IPv4 lab aggregate, normally /16. Each VNet defaults to a distinct /24.')
param labCidr string

param vnetPrefixes {
  hub: string
  spoke1: string
  spoke2: string
  test: string
  endpoints: string
  resolver: string
  standalone: string
  onprem: string
} = {
  hub: cidrSubnet(labCidr, 24, 0)
  spoke1: cidrSubnet(labCidr, 24, 1)
  spoke2: cidrSubnet(labCidr, 24, 2)
  test: cidrSubnet(labCidr, 24, 3)
  endpoints: cidrSubnet(labCidr, 24, 4)
  resolver: cidrSubnet(labCidr, 24, 5)
  standalone: cidrSubnet(labCidr, 24, 6)
  onprem: cidrSubnet(labCidr, 24, 7)
}

param subnetPrefixes {
  hub: string
  spoke1: string
  spoke2: string
  test: string
  endpoints: string
  inbound: string
  outbound: string
  standalone: string
  onprem: string
} = {
  hub: cidrSubnet(vnetPrefixes.hub, 26, 0)
  spoke1: cidrSubnet(vnetPrefixes.spoke1, 26, 0)
  spoke2: cidrSubnet(vnetPrefixes.spoke2, 26, 0)
  test: cidrSubnet(vnetPrefixes.test, 26, 0)
  endpoints: cidrSubnet(vnetPrefixes.endpoints, 26, 0)
  inbound: cidrSubnet(vnetPrefixes.resolver, 28, 0)
  outbound: cidrSubnet(vnetPrefixes.resolver, 28, 1)
  standalone: cidrSubnet(vnetPrefixes.standalone, 26, 0)
  onprem: cidrSubnet(vnetPrefixes.onprem, 26, 0)
}

param inboundIp string = cidrHost(subnetPrefixes.inbound, 4)
param dnsVmIp string = cidrHost(subnetPrefixes.onprem, 4)
param blobEndpointIp string = cidrHost(subnetPrefixes.endpoints, 4)
param vaultEndpointIp string = cidrHost(subnetPrefixes.endpoints, 5)

@description('Optional additional IPv4 client CIDRs permitted to query Windows DNS directly.')
param allowedDnsClientCidrs string[] = []

param adminUsername string = 'labadmin'

@secure()
@minLength(12)
param adminPassword string

param vmSize string = 'Standard_B2s'
param windowsImage {
  publisher: string
  offer: string
  sku: string
  version: string
} = {
  publisher: 'MicrosoftWindowsServer'
  offer: 'WindowsServer'
  sku: '2022-datacenter-azure-edition'
  version: 'latest'
}

param tags object = {
  workload: 'hybrid-dns-lab'
}

var labTags = union(tags, { lab: prefix })

resource networkGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: '${prefix}-network'
  location: location
  tags: labTags
}

resource dnsGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'dns-private-zones'
  location: location
  tags: labTags
}

resource servicesGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: '${prefix}-services'
  location: location
  tags: labTags
}

resource simulationGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: '${prefix}-simulation'
  location: location
  tags: labTags
}

var dnsSources = concat([
  subnetPrefixes.inbound
  subnetPrefixes.outbound
], allowedDnsClientCidrs)

module network './modules/network.bicep' = {
  name: '${prefix}-network'
  scope: networkGroup
  params: {
    location: location
    prefix: prefix
    vnetPrefixes: vnetPrefixes
    subnetPrefixes: subnetPrefixes
    dnsSources: dnsSources
    tags: labTags
  }
}

module zones './modules/private-zones.bicep' = {
  name: '${prefix}-private-zones'
  scope: dnsGroup
  params: {
    vnetIds: network.outputs.participatingVnetIds
    standaloneVnetId: network.outputs.vnetIds.standalone
    tags: labTags
  }
}

module resolver './modules/resolver.bicep' = {
  name: '${prefix}-resolver'
  scope: networkGroup
  params: {
    location: location
    prefix: prefix
    resolverVnetId: network.outputs.vnetIds.resolver
    inboundSubnetId: network.outputs.inboundSubnetId
    outboundSubnetId: network.outputs.outboundSubnetId
    inboundIp: inboundIp
    dnsVmIp: dnsVmIp
    vnetIds: network.outputs.participatingVnetIds
    tags: labTags
  }
}

module services './modules/private-services.bicep' = {
  name: '${prefix}-services'
  scope: servicesGroup
  params: {
    location: location
    prefix: prefix
    endpointSubnetId: network.outputs.endpointSubnetId
    blobEndpointIp: blobEndpointIp
    vaultEndpointIp: vaultEndpointIp
    blobZoneId: zones.outputs.blobZoneId
    vaultZoneId: zones.outputs.vaultZoneId
    standaloneSubnetId: network.outputs.standaloneSubnetId
    registryZoneId: zones.outputs.registryZoneId
    tags: labTags
  }
}

module simulation './modules/windows-dns.bicep' = {
  name: '${prefix}-simulation'
  scope: simulationGroup
  params: {
    location: location
    prefix: prefix
    subnetId: network.outputs.onpremSubnetId
    dnsVmIp: dnsVmIp
    inboundIp: resolver.outputs.inboundIp
    dnsSources: dnsSources
    adminUsername: adminUsername
    adminPassword: adminPassword
    vmSize: vmSize
    windowsImage: windowsImage
    tags: labTags
  }
}

output lab object = {
  prefix: prefix
  tenantId: tenant().tenantId
  subscriptionId: subscription().subscriptionId
  resourceGroups: [
    networkGroup.name
    dnsGroup.name
    servicesGroup.name
    simulationGroup.name
  ]
  vnetIds: network.outputs.vnetIds
  participatingVnetIds: network.outputs.participatingVnetIds
  zoneIds: [
    zones.outputs.blobZoneId
    zones.outputs.vaultZoneId
  ]
  standaloneZoneId: zones.outputs.registryZoneId
  resolverId: resolver.outputs.resolverId
  rulesetId: resolver.outputs.rulesetId
  inboundIp: resolver.outputs.inboundIp
  outboundSubnetPrefix: subnetPrefixes.outbound
  dnsVm: simulation.outputs.vm
  sampleRecord: 'dns-test.demo-lab'
  sampleRecordIp: dnsVmIp
  services: services.outputs.services
}


param location string
param prefix string
param resolverVnetId string
param inboundSubnetId string
param outboundSubnetId string
param inboundIp string
param dnsVmIp string
param vnetIds string[]
param tags object

resource resolver 'Microsoft.Network/dnsResolvers@2025-05-01' = {
  name: '${prefix}-resolver'
  location: location
  tags: tags
  properties: {
    virtualNetwork: { id: resolverVnetId }
  }
}

resource inbound 'Microsoft.Network/dnsResolvers/inboundEndpoints@2025-05-01' = {
  parent: resolver
  name: 'inbound'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        privateIpAllocationMethod: 'Static'
        privateIpAddress: inboundIp
        subnet: { id: inboundSubnetId }
      }
    ]
  }
}

resource outbound 'Microsoft.Network/dnsResolvers/outboundEndpoints@2025-05-01' = {
  parent: resolver
  name: 'outbound'
  location: location
  tags: tags
  properties: {
    subnet: { id: outboundSubnetId }
  }
}

resource ruleset 'Microsoft.Network/dnsForwardingRulesets@2025-05-01' = {
  name: '${prefix}-ruleset'
  location: location
  tags: tags
  properties: {
    dnsResolverOutboundEndpoints: [{ id: outbound.id }]
  }
}

resource rule 'Microsoft.Network/dnsForwardingRulesets/forwardingRules@2025-05-01' = {
  parent: ruleset
  name: 'demo-lab'
  properties: {
    domainName: 'demo-lab.'
    forwardingRuleState: 'Enabled'
    targetDnsServers: [
      { ipAddress: dnsVmIp, port: 53 }
    ]
  }
}

resource links 'Microsoft.Network/dnsForwardingRulesets/virtualNetworkLinks@2025-05-01' = [for (id, i) in vnetIds: {
  parent: ruleset
  name: 'vnet-${i}'
  properties: {
    virtualNetwork: { id: id }
  }
}]

output resolverId string = resolver.id
output rulesetId string = ruleset.id
output inboundIp string = inbound.properties.ipConfigurations[0].privateIpAddress!


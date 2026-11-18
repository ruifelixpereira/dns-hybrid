param location string
param prefix string
param vnetPrefixes object
param subnetPrefixes object
param dnsSources string[]
param tags object

var roles = [
  'hub'
  'spoke1'
  'spoke2'
  'test'
  'endpoints'
  'resolver'
  'standalone'
  'onprem'
]

var participatingRoles = take(roles, 6)
var hubPeers = [
  'spoke1'
  'spoke2'
  'test'
  'endpoints'
  'resolver'
  'onprem'
]
var fromHub = [for role in hubPeers: { source: 'hub', target: role }]
var toHub = [for role in hubPeers: { source: role, target: 'hub' }]
var peerPairs = concat(
  fromHub,
  toHub,
  [
    { source: 'resolver', target: 'onprem' }
    { source: 'onprem', target: 'resolver' }
  ]
)

resource egressIp 'Microsoft.Network/publicIPAddresses@2025-01-01' = {
  name: '${prefix}-dns-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource nat 'Microsoft.Network/natGateways@2025-01-01' = {
  name: '${prefix}-dns-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    publicIpAddresses: [{ id: egressIp.id }]
    idleTimeoutInMinutes: 4
  }
}

resource dnsNsg 'Microsoft.Network/networkSecurityGroups@2025-01-01' = {
  name: '${prefix}-dns'
  location: location
  tags: tags
  properties: {
    securityRules: concat(map(['Tcp', 'Udp'], (protocol, i) => {
      name: 'Allow-DNS-${protocol}'
      properties: {
        priority: 100 + i
        access: 'Allow'
        direction: 'Inbound'
        protocol: protocol
        sourceAddressPrefixes: dnsSources
        sourcePortRange: '*'
        destinationAddressPrefix: '*'
        destinationPortRange: '53'
      }
    }), [
      {
        name: 'Deny-Other-Inbound'
        properties: {
          priority: 200
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ])
  }
}

resource vnets 'Microsoft.Network/virtualNetworks@2025-01-01' = [for role in roles: {
  name: '${prefix}-${role}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetPrefixes[role]] }
    dhcpOptions: { dnsServers: [] }
    subnets: role == 'resolver' ? map(['inbound', 'outbound'], direction => {
      name: direction
      properties: {
        addressPrefix: subnetPrefixes[direction]
        defaultOutboundAccess: false
        delegations: [
          {
            name: 'dns-resolver'
            properties: { serviceName: 'Microsoft.Network/dnsResolvers' }
          }
        ]
      }
    }) : [
      {
        name: 'workload'
        properties: union({
          addressPrefix: subnetPrefixes[role]
          defaultOutboundAccess: false
          privateEndpointNetworkPolicies: role == 'endpoints' ? 'Disabled' : 'Enabled'
        }, role == 'onprem' ? {
          natGateway: { id: nat.id }
          networkSecurityGroup: { id: dnsNsg.id }
        } : {})
      }
    ]
  }
}]

// Serialize peering writes: Azure can reject concurrent updates of the same VNet.
@batchSize(1)
resource peerings 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2025-01-01' = [for pair in peerPairs: {
  name: '${prefix}-${pair.source}/to-${pair.target}'
  properties: {
    remoteVirtualNetwork: {
      id: resourceId('Microsoft.Network/virtualNetworks', '${prefix}-${pair.target}')
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
  dependsOn: [vnets]
}]

output vnetIds object = toObject(roles, role => role, role => resourceId('Microsoft.Network/virtualNetworks', '${prefix}-${role}'))
output participatingVnetIds string[] = [for role in participatingRoles: resourceId('Microsoft.Network/virtualNetworks', '${prefix}-${role}')]
output endpointSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', '${prefix}-endpoints', 'workload')
output inboundSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', '${prefix}-resolver', 'inbound')
output outboundSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', '${prefix}-resolver', 'outbound')
output onpremSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', '${prefix}-onprem', 'workload')


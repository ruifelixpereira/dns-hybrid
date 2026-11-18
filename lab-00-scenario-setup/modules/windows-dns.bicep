param location string
param prefix string
param subnetId string
param dnsVmIp string
param inboundIp string
param dnsSources string[]
param adminUsername string
@secure()
param adminPassword string
param vmSize string
param windowsImage {
  publisher: string
  offer: string
  sku: string
  version: string
}
param tags object

resource nic 'Microsoft.Network/networkInterfaces@2025-01-01' = {
  name: '${prefix}-dns'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Static'
          privateIPAddress: dnsVmIp
          subnet: { id: subnetId }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-11-01' = {
  name: '${prefix}-dns'
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: vmSize }
    storageProfile: {
      imageReference: windowsImage
      osDisk: {
        createOption: 'FromImage'
        deleteOption: 'Delete'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
      }
    }
    osProfile: {
      computerName: 'dns-${take(uniqueString(resourceGroup().id), 8)}'
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        provisionVMAgent: true
        enableAutomaticUpdates: true
        patchSettings: { patchMode: 'AutomaticByOS' }
      }
    }
    networkProfile: {
      networkInterfaces: [
        { id: nic.id, properties: { deleteOption: 'Delete' } }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: { enabled: true }
    }
  }
}

resource configure 'Microsoft.Compute/virtualMachines/runCommands@2024-11-01' = {
  parent: vm
  name: 'configure-dns'
  location: location
  tags: tags
  properties: {
    source: { script: loadTextContent('../scripts/Configure-Dns.ps1') }
    parameters: [
      { name: 'DnsVmIp', value: dnsVmIp }
      { name: 'InboundIp', value: inboundIp }
      { name: 'AllowedSourceCidrs', value: join(dnsSources, ',') }
    ]
    asyncExecution: false
    timeoutInSeconds: 1800
    treatFailureAsDeploymentFailure: true
  }
}

output vm object = {
  id: vm.id
  name: vm.name
  resourceGroup: resourceGroup().name
  privateIp: dnsVmIp
  configurationId: configure.id
}


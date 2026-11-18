using '../main.bicep'

param location = 'westeurope'
param prefix = 'dnslab-b'
param labCidr = '10.20.0.0/16'
param adminPassword = readEnvironmentVariable('DNS_ADMIN_PASSWORD')
param tags = {
  workload: 'hybrid-dns-lab'
  environment: 'tenant-b'
}


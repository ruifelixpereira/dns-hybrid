using '../main.bicep'

param location = 'westeurope'
param prefix = 'dnslab-a'
param labCidr = '10.10.0.0/16'
param adminPassword = readEnvironmentVariable('DNS_ADMIN_PASSWORD')
param tags = {
  workload: 'hybrid-dns-lab'
  environment: 'tenant-a'
}


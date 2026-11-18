[CmdletBinding()]
param(
    [Parameter(Mandatory)][ipaddress]$DnsVmIp,
    [Parameter(Mandatory)][ipaddress]$InboundIp,
    [Parameter(Mandatory)][string]$AllowedSourceCidrs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sources = @($AllowedSourceCidrs.Split(',') | ForEach-Object { $_.Trim() })
if ($sources.Count -eq 0 -or $sources -contains '' -or $sources -contains '0.0.0.0/0') {
    throw 'Specify explicit, nonempty DNS client CIDRs; unrestricted recursion is not allowed.'
}
foreach ($source in $sources) {
    if ($source -notmatch '^(\d{1,3}\.){3}\d{1,3}/(\d|[12]\d|3[0-2])$') {
        throw "Invalid IPv4 client CIDR: $source"
    }
    $null = [ipaddress]::Parse($source.Split('/')[0])
}

$feature = Install-WindowsFeature -Name DNS -IncludeManagementTools
if (-not $feature.Success -or $feature.RestartNeeded -eq 'Yes') {
    throw 'DNS role installation failed or requires a restart. Resolve this and redeploy.'
}
Import-Module DnsServer
Set-Service -Name DNS -StartupType Automatic
Start-Service -Name DNS

$zone = Get-DnsServerZone | Where-Object ZoneName -eq 'demo-lab'
if (-not $zone) {
    Add-DnsServerPrimaryZone -Name 'demo-lab' -ZoneFile 'demo-lab.dns' -DynamicUpdate None
} elseif ($zone.ZoneType -ne 'Primary' -or $zone.IsDsIntegrated) {
    throw 'Existing demo-lab zone is not the expected file-backed primary zone.'
}

$records = @(Get-DnsServerResourceRecord -ZoneName 'demo-lab' |
    Where-Object { $_.HostName -eq 'dns-test' })
if (@($records | Where-Object RecordType -ne 'A').Count -ne 0) {
    throw 'dns-test has a conflicting non-A record; refusing to overwrite it.'
}
if ($records.Count -ne 1 -or $records[0].RecordData.IPv4Address.IPAddressToString -ne $DnsVmIp.IPAddressToString) {
    foreach ($record in $records) {
        Remove-DnsServerResourceRecord -ZoneName 'demo-lab' -InputObject $record -Force
    }
    Add-DnsServerResourceRecordA -ZoneName 'demo-lab' -Name 'dns-test' -IPv4Address $DnsVmIp
}

foreach ($domain in @('blob.core.windows.net', 'vault.azure.net')) {
    $existing = Get-DnsServerZone | Where-Object ZoneName -eq $domain
    if (-not $existing) {
        Add-DnsServerConditionalForwarderZone -Name $domain -MasterServers $InboundIp -ForwarderTimeout 5
    } elseif ($existing.ZoneType -eq 'Forwarder') {
        Set-DnsServerConditionalForwarderZone -Name $domain -MasterServers $InboundIp -ForwarderTimeout 5
    } else {
        throw "Existing zone $domain is not a conditional forwarder."
    }
}
Set-DnsServerForwarder -IPAddress '168.63.129.16' -UseRootHint $false

# Role installation enables broad built-in DNS rules. Replace them with scoped rules.
$dnsRules = @(Get-NetFirewallRule | Where-Object {
    $_.Name -like 'DNS*' -and $_.Direction -eq 'Inbound'
})
$dnsRules | Disable-NetFirewallRule
foreach ($protocol in @('TCP', 'UDP')) {
    $name = "HybridLab-DNS-$protocol"
    $existing = Get-NetFirewallRule | Where-Object Name -eq $name
    if ($existing) {
        $existing | Remove-NetFirewallRule
    }
    New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow `
        -Protocol $protocol -LocalPort 53 -RemoteAddress $sources -Profile Any | Out-Null
}
Set-NetFirewallProfile -Profile Domain, Private, Public -Enabled True -DefaultInboundAction Block
Write-Output "Configured authoritative demo-lab and Azure conditional forwarders. Sample: dns-test.demo-lab = $DnsVmIp"


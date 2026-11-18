Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$configure = Join-Path $PSScriptRoot '../scripts/Configure-Dns.ps1'

$global:zones = @{}
$global:records = @()
$global:rules = @([pscustomobject]@{ Name = 'DNS-UDP-In'; Direction = 'Inbound'; Enabled = $true })
$global:installSuccess = $true
$global:installCalls = 0

function Install-WindowsFeature {
    param($Name, [switch]$IncludeManagementTools)
    $global:installCalls++
    [pscustomobject]@{ Success = $global:installSuccess; RestartNeeded = 'No' }
}
function Import-Module { param($Name) }
function Set-Service { param($Name, $StartupType) }
function Start-Service { param($Name) }
function Get-DnsServerZone { $global:zones.Values }
function Add-DnsServerPrimaryZone {
    param($Name, $ZoneFile, $DynamicUpdate)
    $global:zones[$Name] = [pscustomobject]@{ ZoneName = $Name; ZoneType = 'Primary'; IsDsIntegrated = $false }
}
function Get-DnsServerResourceRecord { param($ZoneName); $global:records }
function Remove-DnsServerResourceRecord {
    param($ZoneName, $InputObject, [switch]$Force)
    $global:records = @($global:records | Where-Object { $_ -ne $InputObject })
}
function Add-DnsServerResourceRecordA {
    param($ZoneName, $Name, [ipaddress]$IPv4Address)
    $global:records += [pscustomobject]@{
        HostName = $Name
        RecordType = 'A'
        RecordData = [pscustomobject]@{ IPv4Address = $IPv4Address }
    }
}
function Add-DnsServerConditionalForwarderZone {
    param($Name, $MasterServers, $ForwarderTimeout)
    $global:zones[$Name] = [pscustomobject]@{
        ZoneName = $Name; ZoneType = 'Forwarder'; MasterServers = $MasterServers
    }
}
function Set-DnsServerConditionalForwarderZone {
    param($Name, $MasterServers, $ForwarderTimeout)
    $global:zones[$Name].MasterServers = $MasterServers
}
function Set-DnsServerForwarder { param($IPAddress, $UseRootHint) }
function Get-NetFirewallRule { $global:rules }
function Disable-NetFirewallRule {
    param([Parameter(ValueFromPipeline)]$InputObject)
    process { $InputObject.Enabled = $false }
}
function Remove-NetFirewallRule {
    param([Parameter(ValueFromPipeline)]$InputObject)
    process { $global:rules = @($global:rules | Where-Object { $_ -ne $InputObject }) }
}
function New-NetFirewallRule {
    param($Name, $DisplayName, $Direction, $Action, $Protocol, $LocalPort, $RemoteAddress, $Profile)
    $global:rules += [pscustomobject]@{
        Name = $Name; Direction = $Direction; Enabled = $true; Sources = $RemoteAddress
    }
}
function Set-NetFirewallProfile { param($Profile, $Enabled, $DefaultInboundAction) }

function Assert($Condition, $Message) {
    if (-not $Condition) { throw $Message }
}
function Assert-Throws([scriptblock]$Action, [string]$Pattern) {
    $caught = $false
    try { & $Action | Out-Null } catch {
        if ($_.Exception.Message -notlike $Pattern) { throw }
        $caught = $true
    }
    Assert $caught "Expected failure matching: $Pattern"
}

$arguments = @{
    DnsVmIp = '10.10.7.4'
    InboundIp = '10.10.5.4'
    AllowedSourceCidrs = '10.10.5.0/28,10.10.5.16/28'
}
& $configure @arguments | Out-Null
& $configure @arguments | Out-Null
Assert ($global:records.Count -eq 1) 'Redeployment duplicated the sample record.'
Assert ($global:zones.Count -eq 3) 'Redeployment duplicated or omitted a zone.'
Assert ($global:zones['blob.core.windows.net'].MasterServers -eq [ipaddress]'10.10.5.4') 'Wrong forwarder.'
$enabled = @($global:rules | Where-Object Enabled)
Assert ($enabled.Count -eq 2) 'Expected only two scoped DNS firewall rules.'
Assert (($enabled[0].Sources -join ',') -eq $arguments.AllowedSourceCidrs) 'Firewall sources were not scoped.'
Assert (-not $global:rules[0].Enabled) 'Built-in broad DNS firewall rule was left enabled.'
Write-Output 'PASS repeatable bootstrap and scoped firewall replacement'

$arguments.DnsVmIp = '10.10.7.5'
$arguments.InboundIp = '10.10.5.5'
& $configure @arguments | Out-Null
Assert ($global:records.Count -eq 1 -and
    $global:records[0].RecordData.IPv4Address -eq [ipaddress]'10.10.7.5') 'Stale record not replaced.'
Assert ($global:zones['vault.azure.net'].MasterServers -eq [ipaddress]'10.10.5.5') 'Stale forwarder not updated.'
Write-Output 'PASS changed addresses update authoritative record and conditional forwarders'

$global:zones['vault.azure.net'].ZoneType = 'Primary'
Assert-Throws { & $configure @arguments } '*not a conditional forwarder*'
$global:zones['vault.azure.net'].ZoneType = 'Forwarder'
$global:installSuccess = $false
Assert-Throws { & $configure @arguments } '*installation failed*'
$global:installSuccess = $true
Write-Output 'PASS conflicting zones and role-install failures surface explicitly'

$before = $global:installCalls
$arguments.AllowedSourceCidrs = '0.0.0.0/0'
Assert-Throws { & $configure @arguments } '*unrestricted recursion*'
Assert ($global:installCalls -eq $before) 'Invalid CIDR triggered side effects.'
Write-Output 'PASS unrestricted DNS source rejected before configuration'

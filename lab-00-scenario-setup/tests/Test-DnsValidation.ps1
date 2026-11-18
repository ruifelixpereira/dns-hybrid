Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$validate = Join-Path $PSScriptRoot '../scripts/Test-Dns.ps1'
$global:negativeMode = 'nxdomain'
$global:wrongBlob = $false

function Resolve-DnsName {
    param($Name, $Server, $Type, [switch]$DnsOnly, [switch]$NoHostsFile, [switch]$TcpOnly)
    if ($Server -eq '168.63.129.16') {
        switch ($global:negativeMode) {
            'nxdomain' { $id = 'DNS_ERROR_RCODE_NAME_ERROR' }
            'timeout' { $id = 'ERROR_TIMEOUT' }
            'positive' { return [pscustomobject]@{ Type = 'A'; IPAddress = '10.10.7.4' } }
        }
        $exception = [System.Exception]::new('Mock DNS error')
        $record = [System.Management.Automation.ErrorRecord]::new(
            $exception, $id, [System.Management.Automation.ErrorCategory]::ResourceUnavailable, $null)
        throw $record
    }
    $ip = switch ($Name) {
        'dns-test.demo-lab.' { '10.10.7.4' }
        'example.blob.core.windows.net.' { if ($global:wrongBlob) { '20.1.1.1' } else { '10.10.4.4' } }
        'example.vault.azure.net.' { '10.10.4.5' }
        default { throw "Unexpected DNS test query: $Name" }
    }
    [pscustomobject]@{ Type = 'A'; IPAddress = $ip }
}

function Assert-Throws([scriptblock]$Action, [string]$Pattern) {
    $caught = $false
    try { & $Action | Out-Null } catch {
        if ($_.Exception.Message -notlike $Pattern) { throw }
        $caught = $true
    }
    if (-not $caught) { throw "Expected failure matching: $Pattern" }
}

$arguments = @{
    InboundIp = '10.10.5.4'
    DnsVmIp = '10.10.7.4'
    BlobFqdn = 'example.blob.core.windows.net'
    BlobIp = '10.10.4.4'
    VaultFqdn = 'example.vault.azure.net'
    VaultIp = '10.10.4.5'
}
& $validate @arguments | Out-Null
Write-Output 'PASS DNS validator accepts expected addresses and explicit NXDOMAIN'
$global:negativeMode = 'timeout'
Assert-Throws { & $validate @arguments } '*Mock DNS error*'
$global:negativeMode = 'positive'
Assert-Throws { & $validate @arguments } '*Unexpected positive*'
Write-Output 'PASS negative control rejects timeouts and positive answers'
$global:negativeMode = 'nxdomain'
$global:wrongBlob = $true
Assert-Throws { & $validate @arguments } '*expected 10.10.4.4*'
Write-Output 'PASS DNS validator rejects public or otherwise incorrect endpoint addresses'

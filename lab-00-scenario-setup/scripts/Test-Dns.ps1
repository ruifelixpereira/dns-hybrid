[CmdletBinding()]
param(
    [Parameter(Mandatory)][ipaddress]$InboundIp,
    [Parameter(Mandatory)][ipaddress]$DnsVmIp,
    [Parameter(Mandatory)][string]$BlobFqdn,
    [Parameter(Mandatory)][ipaddress]$BlobIp,
    [Parameter(Mandatory)][string]$VaultFqdn,
    [Parameter(Mandatory)][ipaddress]$VaultIp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-DnsAddress {
    param([string]$Name, [string]$Server, [ipaddress]$Expected, [switch]$TcpOnly)
    $answers = @(Resolve-DnsName -Name "$Name." -Server $Server -Type A -DnsOnly -NoHostsFile -TcpOnly:$TcpOnly)
    $addresses = @($answers | Where-Object Type -eq 'A' | ForEach-Object IPAddress)
    if ($addresses.Count -ne 1 -or $addresses[0] -ne $Expected.IPAddressToString) {
        throw "$Name via $Server returned [$($addresses -join ',')], expected $Expected."
    }
    Write-Output "PASS DNS $Name via $Server = $Expected (TCP-only: $TcpOnly)"
}

Assert-DnsAddress -Name 'dns-test.demo-lab' -Server $InboundIp.IPAddressToString -Expected $DnsVmIp
Assert-DnsAddress -Name 'dns-test.demo-lab' -Server $InboundIp.IPAddressToString -Expected $DnsVmIp -TcpOnly

$negativeResponse = $false
try {
    $null = Resolve-DnsName -Name 'dns-test.demo-lab.' -Server '168.63.129.16' -Type A -DnsOnly -NoHostsFile
} catch {
    if ($_.FullyQualifiedErrorId -notlike 'DNS_ERROR_RCODE_NAME_ERROR*') {
        throw
    }
    $negativeResponse = $true
}
if (-not $negativeResponse) {
    throw 'Unexpected positive demo-lab response from Azure DNS in the unlinked simulated-on-premises VNet.'
}
Write-Output 'PASS negative control: Azure DNS returned NXDOMAIN for demo-lab in the unlinked VNet.'

Assert-DnsAddress -Name $BlobFqdn -Server '127.0.0.1' -Expected $BlobIp
Assert-DnsAddress -Name $VaultFqdn -Server '127.0.0.1' -Expected $VaultIp
Write-Output 'DNS checks passed. This does not prove TCP reachability or data-plane authorization.'


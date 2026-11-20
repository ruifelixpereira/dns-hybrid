#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('validate', 'what-if', 'create')][string]$Action,
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ResourceGroup,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ResolverVnetId,
    [ValidateNotNullOrEmpty()][string]$InputCsv = './inventory/hub-dns-zones.csv'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$python = Get-Command python3, python -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $python) { throw 'Python 3.10 or later is required' }

& $python.Source (Join-Path $PSScriptRoot 'deploy.py') $Action `
    --tenant-id $TenantId.ToString() `
    --subscription-id $SubscriptionId.ToString() `
    --resource-group $ResourceGroup `
    --resolver-vnet-id $ResolverVnetId `
    --input-csv $InputCsv
exit $LASTEXITCODE

#requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$python = Get-Command python3, python -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $python) { throw 'Python 3.10 or later is required' }

& $python.Source (Join-Path $PSScriptRoot 'align_dns.py') @args
exit $LASTEXITCODE

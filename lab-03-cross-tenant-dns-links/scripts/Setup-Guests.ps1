#requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

& (Join-Path $PSScriptRoot 'Lab.ps1') guests @args
exit $LASTEXITCODE

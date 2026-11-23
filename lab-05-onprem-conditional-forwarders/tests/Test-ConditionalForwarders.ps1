#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '../scripts/Setup-ConditionalForwarders.ps1'
$directory = Join-Path ([IO.Path]::GetTempPath()) ("lab05-tests-" + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $directory
$manifestPath = Join-Path $directory 'manifest.json'
$script:TestCount = 0

function Assert {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-Forwarder {
    param([string]$Name, [string[]]$Servers = @('10.10.1.4', '10.10.1.5'))
    return [pscustomobject]@{
        ZoneName = $Name; ZoneType = 'Forwarder'; MasterServers = $Servers
        IsDsIntegrated = $false; IsPaused = $false; IsShutdown = $false; UseRecursion = $false
        ForwarderTimeout = 5
    }
}

function Reset-Fixture {
    $global:Lab05Store = @{}
    $global:Lab05Writes = [Collections.Generic.List[object]]::new()
    $global:Lab05Reads = 0
    $global:Lab05FailName = ''
    $global:Lab05CorruptWrite = $false
    $global:Lab05InjectOnRead = 0
    $global:Lab05LegacyJson = $false
    $global:Lab05FakePython = $false
    $global:Lab05DiscoveryExit = 0
    $global:Lab05DiscoveryArguments = @()
    $global:Lab05LiveJson = ''
    $subscription = '11111111-1111-1111-1111-111111111111'
    $scope = "/subscriptions/$subscription/resourceGroups/network/providers/Microsoft.Network"
    $script:Manifest = @{
        schemaVersion = 2; generatedAt = '2026-11-23T21:00:00Z'
        centralTenantId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        resolverVnetId = "$scope/virtualNetworks/resolver"
        subscriptionIds = @($subscription); dnsServers = @('10.10.1.4', '10.10.1.5')
        zones = @(
            @{ name = 'a.private'; id = "$scope/privateDnsZones/a.private"; subscriptionId = $subscription },
            @{ name = 'b.private'; id = "$scope/privateDnsZones/b.private"; subscriptionId = $subscription }
        )
    }
}

function Import-Module {
    [CmdletBinding()]
    param([string]$Name)
    Assert ($Name -eq 'DnsServer') 'Unexpected module import'
}

function Set-SourceZones {
    param([string[]]$Names)
    $scope = $script:Manifest.resolverVnetId.Replace('/virtualNetworks/resolver', '/privateDnsZones/')
    $script:Manifest.zones = @(
        foreach ($name in $Names) {
            @{ name = $name; id = "$scope$name"; subscriptionId = $script:Manifest.subscriptionIds[0] }
        }
    )
}

function Get-Command {
    [CmdletBinding()]
    param([string[]]$Name, [Management.Automation.CommandTypes]$CommandType)
    if ($global:Lab05FakePython -and $Name -contains 'python3') {
        return [pscustomobject]@{ Source = 'Invoke-Lab05Discovery' }
    }
    if ($global:Lab05LegacyJson -and $Name -contains 'ConvertFrom-Json') {
        return [pscustomobject]@{ Parameters = @{} }
    }
    return Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}

function Invoke-Lab05Discovery {
    $global:Lab05DiscoveryArguments = @($args)
    $global:LASTEXITCODE = $global:Lab05DiscoveryExit
    if ($global:Lab05DiscoveryExit -ne 0) { return }
    $data = $global:Lab05LiveJson | ConvertFrom-Json
    $index = [array]::IndexOf($args, '--inbound-ip')
    Assert ($index -ge 0) 'Python helper did not receive --inbound-ip'
    $data.dnsServers = @($args[($index + 1)..($args.Count - 1)])
    return $data | ConvertTo-Json -Depth 10
}

function Get-DnsServerZone {
    [CmdletBinding()]
    param([string]$ComputerName, [string]$Name)
    Assert ($ComputerName -eq 'dns-test') 'Wrong Windows DNS server selected'
    $global:Lab05Reads++
    if ($global:Lab05Reads -eq $global:Lab05InjectOnRead) { $global:Lab05Store['a.private'] = New-Forwarder 'a.private' }
    if ($PSBoundParameters.ContainsKey('Name')) {
        if (-not $global:Lab05Store.ContainsKey($Name)) { throw "Zone not found: $Name" }
        return $global:Lab05Store[$Name]
    }
    return @($global:Lab05Store.Values)
}

function Add-DnsServerConditionalForwarderZone {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [string]$Name, [ipaddress[]]$MasterServers,
          [uint32]$ForwarderTimeout, [switch]$UseRecursion)
    $global:Lab05Writes.Add(@{
        ComputerName = $ComputerName; Name = $Name; MasterServers = @($MasterServers | ForEach-Object IPAddressToString)
        Timeout = $ForwarderTimeout; Recursion = $UseRecursion.IsPresent
    })
    if ($Name -eq $global:Lab05FailName) { throw 'Simulated DNS write failure' }
    Assert (-not $global:Lab05Store.ContainsKey($Name)) 'Attempted to overwrite an existing DNS zone'
    $servers = @($MasterServers | ForEach-Object IPAddressToString)
    if ($global:Lab05CorruptWrite) { $servers = @('8.8.8.8') }
    $global:Lab05Store[$Name] = New-Forwarder $Name $servers
}

function Invoke-Workflow {
    param([hashtable]$Options = @{})
    $script:Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $global:LASTEXITCODE = 0
    $output = @(& $scriptPath -InputFile $manifestPath -DnsServer 'dns-test' -Confirm:$false @Options *>&1)
    return @{ Code = $LASTEXITCODE; Output = $output -join "`n" }
}

try {
    Reset-Fixture
    $result = Invoke-Workflow @{ WhatIf = $true }
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 0) "WhatIf must not write DNS configuration: $($result.Output)"
    $script:TestCount++

    Reset-Fixture
    Set-SourceZones @(
        'privatelink.blob.core.windows.net', 'privatelink.file.core.windows.net',
        'privatelink.database.windows.net', 'privatelink.vaultcore.azure.net'
    )
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 5) "Public domain mapping failed: $($result.Output)"
    $actual = @($global:Lab05Writes | ForEach-Object { $_.Name })
    Assert (($actual -join ',') -eq 'blob.core.windows.net,database.windows.net,file.core.windows.net,vault.azure.net,vaultcore.azure.net') 'Storage/SQL/Key Vault public domains are incorrect'
    $script:TestCount++

    Reset-Fixture
    Set-SourceZones @('privatelink.blob.core.windows.net', 'privatelink.file.core.windows.net')
    $existing = New-Forwarder 'blob.core.windows.net' @('8.8.8.8')
    $oldPrivate = New-Forwarder 'privatelink.file.core.windows.net' @('8.8.4.4')
    $global:Lab05Store['blob.core.windows.net'] = $existing
    $global:Lab05Store['privatelink.file.core.windows.net'] = $oldPrivate
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 1 -and
        $global:Lab05Writes[0].Name -eq 'file.core.windows.net') 'Existing public rules were not skipped correctly'
    Assert ([object]::ReferenceEquals($existing, $global:Lab05Store['blob.core.windows.net'])) 'Existing public forwarder was changed'
    Assert ([object]::ReferenceEquals($oldPrivate, $global:Lab05Store['privatelink.file.core.windows.net'])) 'Old private forwarder was changed'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(1\):\r?\n  blob\.core\.windows\.net$') 'Skipped list must use public forwarding domains'
    $script:TestCount++

    Reset-Fixture
    Set-SourceZones @('privatelink.wvd.microsoft.com', 'privatelink-global.wvd.microsoft.com')
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 1 -and
        $global:Lab05Writes[0].Name -eq 'wvd.microsoft.com') 'Shared public forwarding domains were not deduplicated'
    $script:TestCount++

    Reset-Fixture
    Set-SourceZones @(
        'eastus.data.privatelink.azurecr.io', 'cluster.privatelink.westeurope.azmk8s.io',
        'privatelink.westeurope.azurecontainerapps.io'
    )
    $result = Invoke-Workflow
    $actual = @($global:Lab05Writes | ForEach-Object { $_.Name })
    Assert ($result.Code -eq 0 -and ($actual -join ',') -eq
        'azurecontainerapps.io,eastus.data.azurecr.io,westeurope.azmk8s.io') "Regional mapping failed: $($result.Output)"
    $script:TestCount++

    Reset-Fixture
    Set-SourceZones @('privatelink.unsupported.example')
    $result = Invoke-Workflow
    Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 0 -and $global:Lab05Reads -eq 0) 'Unknown Private Link mapping must fail before DNS reads/writes'
    Assert ($result.Output -match 'ForwardingDomainMap') 'Unknown mapping error must explain the override'
    $script:TestCount++
    $result = Invoke-Workflow @{
        ForwardingDomainMap = @{ 'privatelink.unsupported.example' = @('region.service.example', 'api.service.example') }
    }
    $actual = @($global:Lab05Writes | ForEach-Object { $_.Name })
    Assert ($result.Code -eq 0 -and ($actual -join ',') -eq
        'api.service.example,region.service.example') "Explicit public domain mapping failed: $($result.Output)"
    $script:TestCount++

    foreach ($targets in @(@(), @('privatelink.unsupported.example'), @('-invalid.example'))) {
        Reset-Fixture
        $result = Invoke-Workflow @{ ForwardingDomainMap = @{ 'privatelink.unsupported.example' = $targets } }
        Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 0 -and $global:Lab05Reads -eq 0) 'Invalid mapping reached DNS operations'
        $script:TestCount++
    }

    Reset-Fixture
    $script:Manifest.schemaVersion = 1
    $script:Manifest.inboundEndpointId = $script:Manifest.resolverVnetId.Replace(
        '/virtualNetworks/resolver', '/dnsResolvers/resolver/inboundEndpoints/inbound')
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 2) "Legacy manifest failed: $($result.Output)"
    $script:TestCount++

    Reset-Fixture
    $global:Lab05FakePython = $true
    $global:Lab05LiveJson = $script:Manifest | ConvertTo-Json -Depth 10
    $liveOptions = @{
        CentralTenantId = $script:Manifest.centralTenantId
        ResolverVnetId = $script:Manifest.resolverVnetId
        InboundIp = @('10.10.1.5', '10.10.1.4')
        DnsServer = 'dns-test'
        Confirm = $false
    }
    $global:LASTEXITCODE = 0
    $output = @(& $scriptPath @liveOptions *>&1)
    Assert ($LASTEXITCODE -eq 0 -and $global:Lab05Writes.Count -eq 2) "Live IP input failed: $($output -join "`n")"
    foreach ($write in $global:Lab05Writes) {
        Assert (($write.MasterServers -join ',') -eq '10.10.1.5,10.10.1.4') 'Live input IP order was not preserved'
    }
    Assert (-not ($global:Lab05DiscoveryArguments -contains '--inbound-endpoint-id')) 'Obsolete endpoint ID parameter was passed'
    $script:TestCount++
    $global:Lab05Writes.Clear()
    $global:Lab05Reads = 0
    $global:Lab05DiscoveryExit = 1
    $output = @(& $scriptPath @liveOptions *>&1)
    Assert ($LASTEXITCODE -ne 0 -and $global:Lab05Writes.Count -eq 0 -and $global:Lab05Reads -eq 0) 'Discovery failure reached Windows DNS'
    $script:TestCount++

    Reset-Fixture
    $global:Lab05LegacyJson = $true
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 2) "Legacy JSON date conversion failed: $($result.Output)"
    $script:TestCount++
    Reset-Fixture

    $global:Lab05Store['unrelated.example'] = [pscustomobject]@{ ZoneName = 'unrelated.example'; ZoneType = 'Primary' }
    $unrelated = $global:Lab05Store['unrelated.example']
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 2) "Creation failed: $($result.Output)"
    foreach ($write in $global:Lab05Writes) {
        Assert ($write.ComputerName -eq 'dns-test' -and $write.Timeout -eq 5 -and -not $write.Recursion) 'Unexpected DNS write arguments'
        Assert (($write.MasterServers -join ',') -eq '10.10.1.4,10.10.1.5') 'DNS server priority was changed'
    }
    Assert ([object]::ReferenceEquals($unrelated, $global:Lab05Store['unrelated.example'])) 'Unrelated zone was changed'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(0\):\r?\n  None$') 'Empty skipped list is missing'
    $script:TestCount++

    $global:Lab05Writes.Clear()
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 0) 'Matching forwarders must be skipped on rerun'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(2\):\r?\n  a\.private\r?\n  b\.private$') 'Skipped list must contain every existing rule in sorted order'
    $script:TestCount++

    foreach ($conflict in @('Primary', 'DifferentIPs', 'DifferentOrder', 'ADIntegrated', 'Recursion', 'Paused')) {
        Reset-Fixture
        $zone = New-Forwarder 'b.private'
        switch ($conflict) {
            'Primary' { $zone.ZoneType = 'Primary' }
            'DifferentIPs' { $zone.MasterServers = @('8.8.8.8') }
            'DifferentOrder' { $zone.MasterServers = @('10.10.1.5', '10.10.1.4') }
            'ADIntegrated' { $zone.IsDsIntegrated = $true }
            'Recursion' { $zone.UseRecursion = $true }
            'Paused' { $zone.IsPaused = $true }
        }
        $global:Lab05Store['b.private'] = $zone
        $result = Invoke-Workflow
        if ($conflict -eq 'Primary') {
            Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 0) 'Authoritative zone conflict did not stop all writes'
        } else {
            Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 1 -and
                $global:Lab05Writes[0].Name -eq 'a.private') "Existing forwarder prevented missing rule creation: $conflict"
            Assert ([object]::ReferenceEquals($zone, $global:Lab05Store['b.private'])) "Existing forwarder was replaced: $conflict"
            Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(1\):\r?\n  b\.private$') "Skipped rule is missing from final list: $conflict"
        }
        $script:TestCount++
    }

    Reset-Fixture
    $global:Lab05Store['a.private'] = New-Forwarder 'a.private' @('8.8.8.8')
    $result = Invoke-Workflow @{ WhatIf = $true }
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 0) 'WhatIf must skip existing rules without creating missing ones'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(1\):\r?\n  a\.private$') 'WhatIf skipped list is missing'
    $script:TestCount++

    foreach ($invalid in @('DuplicateZone', 'InvalidIP', 'ForeignEndpoint', 'WrongZoneSubscription', 'NonArray')) {
        Reset-Fixture
        switch ($invalid) {
            'DuplicateZone' { $script:Manifest.zones += $script:Manifest.zones[0] }
            'InvalidIP' { $script:Manifest.dnsServers = @('168.63.129.16') }
            'ForeignEndpoint' {
                $script:Manifest.schemaVersion = 1
                $script:Manifest.inboundEndpointId = $script:Manifest.resolverVnetId.Replace(
                    '/virtualNetworks/resolver', '/dnsResolvers/resolver/inboundEndpoints/inbound').Replace('11111111', '22222222')
            }
            'WrongZoneSubscription' { $script:Manifest.zones[0].subscriptionId = '22222222-2222-2222-2222-222222222222' }
            'NonArray' { $script:Manifest.zones = $script:Manifest.zones[0] }
        }
        $result = Invoke-Workflow
        Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 0 -and $global:Lab05Reads -eq 0) "Invalid input reached DNS operations: $invalid"
        $script:TestCount++
    }

    Reset-Fixture
    $script:Manifest.zones = @()
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 0) 'Empty Azure selection must not change Windows DNS'
    $script:TestCount++

    Reset-Fixture
    $global:Lab05InjectOnRead = 2
    $result = Invoke-Workflow
    Assert ($result.Code -eq 0 -and $global:Lab05Writes.Count -eq 1 -and
        $global:Lab05Writes[0].Name -eq 'b.private') 'Forwarder appearing after preview must be skipped while missing rules continue'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(1\):\r?\n  a\.private$') 'Late-appearing forwarder is missing from skipped list'
    $script:TestCount++

    Reset-Fixture
    $global:Lab05Store['a.private'] = New-Forwarder 'a.private' @('8.8.8.8')
    $global:Lab05FailName = 'b.private'
    $result = Invoke-Workflow
    Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 1) 'DNS write failure was swallowed after skipping a rule'
    Assert ($result.Output -match '(?m)^Skipped existing conditional forwarders \(1\):\r?\n  a\.private$') 'Skipped list is missing after partial failure'
    $script:TestCount++

    Reset-Fixture
    $global:Lab05FailName = 'b.private'
    $result = Invoke-Workflow
    Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 2 -and $global:Lab05Store.ContainsKey('a.private')) 'Partial failure state was not preserved'
    Assert ($result.Output -match 'not rolled back') 'Partial failure warning is missing'
    $script:TestCount++

    Reset-Fixture
    $global:Lab05CorruptWrite = $true
    $result = Invoke-Workflow
    Assert ($result.Code -ne 0 -and $global:Lab05Writes.Count -eq 1) 'Readback mismatch did not stop subsequent writes'
    $script:TestCount++

    Write-Output "Passed $script:TestCount offline PowerShell workflow cases."
} finally {
    Remove-Item -LiteralPath $manifestPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $directory
    Remove-Variable -Scope Global -Name Lab05Store, Lab05Writes, Lab05Reads, Lab05FailName, Lab05CorruptWrite, Lab05InjectOnRead, Lab05LegacyJson
    Remove-Variable -Scope Global -Name Lab05FakePython, Lab05DiscoveryExit, Lab05DiscoveryArguments, Lab05LiveJson
}

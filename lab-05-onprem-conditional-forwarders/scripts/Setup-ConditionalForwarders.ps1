#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High', DefaultParameterSetName = 'Live')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Live')][guid]$CentralTenantId,
    [Parameter(Mandatory, ParameterSetName = 'Live')][ValidateNotNullOrEmpty()][string]$ResolverVnetId,
    [Parameter(Mandatory, ParameterSetName = 'Live')][ValidateNotNullOrEmpty()][string[]]$InboundIp,
    [Parameter(Mandatory, ParameterSetName = 'File')][ValidateNotNullOrEmpty()][string]$InputFile,
    [ValidateNotNullOrEmpty()][string]$DnsServer = $env:COMPUTERNAME,
    [hashtable]$ForwardingDomainMap = @{}
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$skipped = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$publicDomains = @{}
foreach ($suffix in @(
    'blob.core.windows.net', 'file.core.windows.net', 'dfs.core.windows.net',
    'queue.core.windows.net', 'table.core.windows.net', 'web.core.windows.net',
    'database.windows.net', 'servicebus.windows.net', 'documents.azure.com',
    'mongo.cosmos.azure.com', 'postgres.database.azure.com', 'mysql.database.azure.com',
    'azurecr.io', 'azconfig.io', 'managedhsm.azure.net', 'redis.cache.windows.net',
    'search.windows.net', 'cognitiveservices.azure.com', 'openai.azure.com', 'services.ai.azure.com'
)) {
    $publicDomains["privatelink.$suffix"] = @($suffix)
}
$publicDomains['privatelink.vaultcore.azure.net'] = @('vault.azure.net', 'vaultcore.azure.net')
$publicDomains['privatelink.azurewebsites.net'] = @('azurewebsites.net', 'scm.azurewebsites.net')
$publicDomains['scm.privatelink.azurewebsites.net'] = @('scm.azurewebsites.net')
$publicDomains['privatelink.eventgrid.azure.net'] = @('eventgrid.azure.net')
$publicDomains['privatelink.ts.eventgrid.azure.net'] = @('eventgrid.azure.net')
$publicDomains['privatelink.wvd.microsoft.com'] = @('wvd.microsoft.com')
$publicDomains['privatelink-global.wvd.microsoft.com'] = @('wvd.microsoft.com')
$overrides = @{}
$privateLinkPattern = '(^|\.)privatelink(?:-global)?(\.|$)'

function Get-DomainName {
    param([string]$Value)
    $name = $Value.TrimEnd('.').ToLowerInvariant()
    if (-not $name -or $name.Length -gt 253) { throw "Invalid DNS domain: $Value" }
    foreach ($label in $name.Split('.')) {
        if ($label -notmatch '^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$') {
            throw "Invalid DNS domain label: $Value"
        }
    }
    return $name
}

function Get-ForwardingDomains {
    param([string]$Name)
    if ($overrides.ContainsKey($Name)) { return $overrides[$Name] }
    if ($publicDomains.ContainsKey($Name)) { return $publicDomains[$Name] }
    if ($Name -match '^(?<region>[a-z0-9-]+)\.data\.privatelink\.azurecr\.io$') {
        return "$($Matches.region).data.azurecr.io"
    }
    if ($Name -match '^(?:[a-z0-9_-]+\.)?privatelink\.(?<region>[a-z0-9-]+)\.azmk8s\.io$') {
        return "$($Matches.region).azmk8s.io"
    }
    if ($Name -match '^privatelink\.[a-z0-9-]+\.azurecontainerapps\.io$') {
        return 'azurecontainerapps.io'
    }
    if ($Name -match $privateLinkPattern) {
        throw "No documented public forwarding mapping for $Name; supply -ForwardingDomainMap"
    }
    return $Name
}

function Get-RequiredText {
    param($Object, [string]$Name)
    if ($null -eq $Object) { throw "Missing object containing $Name" }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace($property.Value)) {
        throw "Expected a nonempty string: $Name"
    }
    return $property.Value
}

function Get-RequiredArray {
    param($Object, [string]$Name)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [array]) {
        throw "Expected a JSON array: $Name"
    }
    return ,$property.Value
}

function Get-ZoneCatalog {
    $catalog = @{}
    foreach ($zone in @(Get-DnsServerZone -ComputerName $DnsServer -ErrorAction Stop)) {
        $key = (Get-RequiredText $zone 'ZoneName').TrimEnd('.').ToLowerInvariant()
        if ($catalog.ContainsKey($key)) { throw "DNS server returned duplicate zone names: $key" }
        $catalog[$key] = $zone
    }
    return $catalog
}

function Assert-MatchingForwarder {
    param($Zone, [string]$Name, [string[]]$ExpectedServers)
    if ($Zone.ZoneType -ne 'Forwarder' -or $Zone.IsDsIntegrated -or
        $Zone.IsPaused -or $Zone.IsShutdown -or $Zone.UseRecursion) {
        throw "Conflicting zone ${Name}: expected an active, local conditional forwarder without recursion fallback"
    }
    $actual = @($Zone.MasterServers | ForEach-Object { ([ipaddress]$_).IPAddressToString })
    if ($actual.Count -ne $ExpectedServers.Count) { throw "Conflicting master servers for $Name" }
    for ($index = 0; $index -lt $actual.Count; $index++) {
        if ($actual[$index] -cne $ExpectedServers[$index]) { throw "Conflicting master servers for $Name" }
    }
}

function Skip-ExistingForwarder {
    param($Zone, [string]$Name)
    if ($Zone.ZoneType -ne 'Forwarder') {
        throw "Existing zone $Name is not a conditional forwarder; refusing to overwrite it"
    }
    $null = $skipped.Add($Name)
    Write-Host "Skip existing conditional forwarder: $Name"
}

try {
    if ([string]::IsNullOrWhiteSpace($DnsServer)) { throw 'Specify -DnsServer when no local computer name is available' }
    if ($null -eq $ForwardingDomainMap) { throw 'ForwardingDomainMap must be a hashtable' }
    foreach ($entry in $ForwardingDomainMap.GetEnumerator()) {
        if ($entry.Key -isnot [string]) { throw 'ForwardingDomainMap keys must be domain strings' }
        $source = Get-DomainName $entry.Key
        if ($overrides.ContainsKey($source)) { throw "Duplicate forwarding mapping: $source" }
        $targets = @($entry.Value)
        if ($targets.Count -eq 0) { throw "Forwarding mapping has no target domains: $source" }
        $overrides[$source] = @(
            foreach ($value in $targets) {
                if ($value -isnot [string]) { throw "Forwarding mapping targets must be strings: $source" }
                $domain = Get-DomainName $value
                if ($domain -match $privateLinkPattern) { throw "Expected a public forwarding domain, not $domain" }
                $domain
            }
        )
    }
    if ($PSCmdlet.ParameterSetName -eq 'File') {
        $json = Get-Content -LiteralPath $InputFile -Raw -Encoding UTF8
    } else {
        $python = Get-Command python3, python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $python) { throw 'Live discovery requires Python 3.10+ and Azure CLI; alternatively use -InputFile' }
        $arguments = @(
            (Join-Path $PSScriptRoot 'export_zones.py'), 'discover',
            '--central-tenant-id', $CentralTenantId.ToString(),
            '--resolver-vnet-id', $ResolverVnetId, '--inbound-ip'
        ) + @($InboundIp)
        $output = @(& $python.Source @arguments)
        if ($LASTEXITCODE -ne 0) { throw "Azure discovery failed with exit code $LASTEXITCODE" }
        $json = $output -join "`n"
    }
    $jsonOptions = @{}
    # Newer PowerShell versions otherwise convert JSON timestamps into DateTime objects.
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $jsonOptions.DateKind = 'String' }
    $manifest = $json | ConvertFrom-Json @jsonOptions
    if (($manifest.schemaVersion -isnot [int] -and $manifest.schemaVersion -isnot [long]) -or
        $manifest.schemaVersion -notin @(1, 2)) { throw 'Unsupported manifest schemaVersion' }
    $tenant = [guid](Get-RequiredText $manifest 'centralTenantId')
    if ($manifest.generatedAt -is [datetime]) {
        if ($manifest.generatedAt.Kind -eq [DateTimeKind]::Unspecified) { throw 'Manifest generatedAt must include a timezone' }
        $generated = [datetimeoffset]$manifest.generatedAt.ToUniversalTime()
    } else {
        $generated = [datetimeoffset](Get-RequiredText $manifest 'generatedAt')
    }
    if ($generated.Offset -ne [timespan]::Zero) { throw 'Manifest generatedAt must use UTC' }
    $resolver = Get-RequiredText $manifest 'resolverVnetId'
    $vnetPattern = '^/subscriptions/(?<subscription>[^/]+)/resourceGroups/(?<group>[^/]+)/providers/Microsoft\.Network/virtualNetworks/[^/]+$'
    $vnet = [regex]::Match($resolver, $vnetPattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $vnet.Success) { throw 'Invalid resolverVnetId' }
    $resolverSubscription = [guid]$vnet.Groups['subscription'].Value
    if ($manifest.schemaVersion -eq 1) {
        $endpoint = Get-RequiredText $manifest 'inboundEndpointId'
        $endpointScope = "/subscriptions/$resolverSubscription/resourceGroups/$($vnet.Groups['group'].Value)"
        $endpointPattern = '^' + [regex]::Escape($endpointScope) +
            '/providers/Microsoft\.Network/dnsResolvers/[^/]+/inboundEndpoints/[^/]+$'
        if ($endpoint -notmatch $endpointPattern) { throw 'Inbound endpoint ID is outside the resolver subscription/resource group' }
    }
    $coverage = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($subscription in (Get-RequiredArray $manifest 'subscriptionIds')) {
        if ($subscription -isnot [string] -or -not $coverage.Add(([guid]$subscription).ToString())) {
            throw 'Invalid or duplicate manifest subscriptionIds'
        }
    }
    if (-not $coverage.Contains($resolverSubscription.ToString())) { throw 'Resolver subscription is outside manifest coverage' }
    $servers = @()
    foreach ($value in (Get-RequiredArray $manifest 'dnsServers')) {
        if ($value -isnot [string]) { throw 'DNS server IPs must be strings' }
        $address = [ipaddress]::Parse($value)
        $bytes = $address.GetAddressBytes()
        if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
            $address.IPAddressToString -cne $value -or $bytes[0] -eq 0 -or $bytes[0] -eq 127 -or
            $bytes[0] -ge 224 -or ($bytes[0] -eq 169 -and $bytes[1] -eq 254) -or
            $value -eq '168.63.129.16' -or $servers -contains $value) {
            throw "Invalid or duplicate resolver inbound IPv4 address: $value"
        }
        $servers += $value
    }
    if ($servers.Count -eq 0) { throw 'Manifest contains no resolver inbound IPs' }
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $zoneIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($zone in (Get-RequiredArray $manifest 'zones')) {
        $name = Get-DomainName (Get-RequiredText $zone 'name')
        if (-not $names.Add($name)) { throw "Duplicate zone name: $name" }
        $id = Get-RequiredText $zone 'id'
        $zonePattern = '^/subscriptions/(?<subscription>[^/]+)/resourceGroups/[^/]+/providers/Microsoft\.Network/privateDnsZones/(?<name>[^/]+)$'
        $match = [regex]::Match($id, $zonePattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if (-not $match.Success -or -not $zoneIds.Add($id)) { throw "Invalid or duplicate private DNS zone ID: $id" }
        $subscription = ([guid](Get-RequiredText $zone 'subscriptionId')).ToString()
        if (-not $coverage.Contains($subscription) -or
            ([guid]$match.Groups['subscription'].Value).ToString() -ne $subscription -or
            $match.Groups['name'].Value.ToLowerInvariant() -ne $name) {
            throw "Private DNS zone name/subscription disagrees with manifest coverage: $name"
        }
    }
    $forwardingNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in @($names | Sort-Object)) {
        $domains = @(Get-ForwardingDomains $name)
        Write-Host "Map linked private zone: $name -> $($domains -join ', ')"
        foreach ($domain in $domains) { $null = $forwardingNames.Add($domain) }
    }
    Import-Module DnsServer -ErrorAction Stop
    $catalog = Get-ZoneCatalog
    $missing = @()
    Write-Host "Conditional forwarder preview: DNS server $DnsServer; central tenant $tenant"
    Write-Host "Source collected at $generated; target IPs $($servers -join ', ')"
    Write-Host 'Verify these IPs belong to the central resolver inbound endpoint; no endpoint lookup is made.'
    foreach ($name in @($forwardingNames | Sort-Object)) {
        if ($catalog.ContainsKey($name)) {
            Skip-ExistingForwarder $catalog[$name] $name
        } else {
            $missing += $name
            Write-Host "Create local forwarder: $name -> $($servers -join ', ')"
        }
    }
    Write-Host "$($missing.Count) to create. Unrelated zones and global DNS settings remain untouched."
    foreach ($name in $missing) {
        if (-not $PSCmdlet.ShouldProcess("$DnsServer / $name", "Create local conditional forwarder to $($servers -join ', ')")) {
            continue
        }
        $live = Get-ZoneCatalog
        if ($live.ContainsKey($name)) {
            Skip-ExistingForwarder $live[$name] $name
            continue
        }
        Write-Host "Creating conditional forwarder: $name"
        Add-DnsServerConditionalForwarderZone -ComputerName $DnsServer -Name $name `
            -MasterServers $servers -ForwarderTimeout 5 -UseRecursion:$false -Confirm:$false `
            -ErrorAction Stop | Out-Null
        $created = @(Get-DnsServerZone -ComputerName $DnsServer -Name $name -ErrorAction Stop)
        if ($created.Count -ne 1) { throw "Cannot verify newly created forwarder: $name" }
        Assert-MatchingForwarder $created[0] $name $servers
        if ($created[0].ForwarderTimeout -ne 5) { throw "Cannot verify forwarder timeout: $name" }
        Write-Host "Created and verified local forwarder: $name"
    }
} catch {
    Write-Error "Stopped: $($_.Exception.Message). Earlier additions are not rolled back; review creation output and live DNS zones." `
        -ErrorAction Continue
    exit 1
} finally {
    Write-Host "Skipped existing conditional forwarders ($($skipped.Count)):"
    if ($skipped.Count -eq 0) { Write-Host '  None' }
    foreach ($name in @($skipped | Sort-Object)) { Write-Host "  $name" }
}

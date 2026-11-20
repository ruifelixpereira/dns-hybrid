#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [ValidateNotNullOrEmpty()][string]$OutputDirectory = './inventory'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$stage = $null
$context = 'initialization'
$inventoryFile = "${TenantId}-inventory.json"

function Get-Text($Value) {
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        throw 'Expected nonempty string'
    }
    return $Value
}

function Get-OptionalText($Value) {
    if ($null -eq $Value) { return $null }
    return Get-Text $Value
}

function Get-Boolean($Value, [switch]$Optional) {
    if ($Optional -and $null -eq $Value) { return $null }
    if ($Value -isnot [bool]) { throw 'Expected boolean' }
    return $Value
}

function Get-Identity($Resource) {
    $result = @{ name = (Get-Text $Resource.name); id = (Get-Text $Resource.id) }
    if ($result.id -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/[^/]+/[^/]+/[^/]+(/[^/]+/[^/]+)*$') {
        throw "Invalid ARM resource ID: $($result.id)"
    }
    return $result
}

function Get-Resource($Resource, [string]$Kind, [string]$Subscription) {
    $result = Get-Identity $Resource
    if ($result.id -notmatch "^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Network/$Kind/[^/]+$") {
        throw "Invalid resource ID: $($result.id)"
    }
    if ($Matches[1] -ine $Subscription) { throw 'Resource subscription mismatch' }
    $result.subscriptionId = $Matches[1]
    $result.resourceGroup = $Matches[2]
    return $result
}

function Get-VnetReference($Reference) {
    $id = Get-Text $Reference.id
    if ($id -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Network/virtualNetworks/([^/]+)/*$') {
        throw "Invalid VNet reference: $id"
    }
    return @{ name = $Matches[1]; id = $id }
}

function Invoke-AzJson([string[]]$Arguments) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        & az @Arguments --only-show-errors --output json 1> "$stage/az-output" 2> "$stage/az-error"
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            $data = Get-Content -LiteralPath "$stage/az-output" -Raw |
                ConvertFrom-Json -AsHashtable -NoEnumerate
            if ($null -eq $data) { throw 'Azure CLI returned null JSON' }
            return ,$data
        }
        $detail = Get-Content -LiteralPath "$stage/az-error" -Raw
        if ($attempt -lt 3 -and $detail -match '(?i)(^|[^a-z0-9])(429|500|502|503|504)([^a-z0-9]|$)|TooManyRequests|ServiceUnavailable|InternalServerError|GatewayTimeout') {
            [Console]::Error.WriteLine("Retrying $context (attempt $($attempt + 1)/3)")
            Start-Sleep -Seconds $attempt
        } else {
            throw "Azure CLI failed (exit $code): $detail"
        }
    }
}

function Get-AllPages([string]$Url, [string]$Subscription) {
    $items = [System.Collections.Generic.List[object]]::new()
    $visited = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    while ($Url) {
        if (-not $Url.StartsWith("$endpoint/subscriptions/$Subscription/", [StringComparison]::Ordinal)) {
            throw "Unexpected pagination URL: $Url"
        }
        if (-not $visited.Add($Url)) { throw 'Pagination cycle' }
        $page = Invoke-AzJson -Arguments @('rest', '--method', 'get', '--url', $Url, '--subscription', $Subscription)
        if ($page -isnot [System.Collections.IDictionary] -or $page['value'] -isnot [array] -or
            ($null -ne $page['nextLink'] -and $page['nextLink'] -isnot [string])) {
            throw 'Invalid list response'
        }
        foreach ($item in $page.value) { $items.Add($item) }
        $Url = $page['nextLink']
    }
    return ,($items.ToArray())
}

function Get-ResourcePath([string]$Id) {
    return (($Id -split '/' | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
}

function Sort-Resources([object[]]$Items) {
    $sorted = [System.Collections.Generic.List[object]]::new()
    foreach ($item in $Items) { $sorted.Add($item) }
    $sorted.Sort([Comparison[object]]{
        param($Left, $Right)
        return [StringComparer]::OrdinalIgnoreCase.Compare($Left.id, $Right.id)
    })
    return ,($sorted.ToArray())
}

function Write-JsonFile($Value, [string]$Path) {
    $json = ConvertTo-Json -InputObject $Value -Depth 50
    [IO.File]::WriteAllText($Path, $json + "`n", [Text.UTF8Encoding]::new($false))
}

try {
    $null = Get-Command az -ErrorAction Stop
    $null = [IO.Directory]::CreateDirectory($OutputDirectory)
    $OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
    if ([IO.Directory]::Exists((Join-Path $OutputDirectory $inventoryFile))) {
        throw "Output is a directory: $inventoryFile"
    }
    $inventoryId = [guid]::NewGuid().ToString()
    $stage = Join-Path $OutputDirectory ".inventory.$inventoryId"
    $null = [IO.Directory]::CreateDirectory($stage)
    $started = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
    $context = 'subscription discovery'
    $accounts = Invoke-AzJson -Arguments @('account', 'list', '--all', '--refresh')
    if ($accounts -isnot [array]) { throw 'Invalid subscription discovery response' }
    $subscriptions = [System.Collections.Generic.List[object]]::new()
    $excluded = [System.Collections.Generic.List[object]]::new()
    foreach ($account in $accounts) {
        $null = Get-Text $account.name
        $null = Get-Text $account.tenantId
        $null = Get-Text $account.state
        $null = [guid]::Parse((Get-Text $account.id))
        if ($account.tenantId -ine $TenantId.ToString()) { continue }
        $entry = @{name = $account.name; id = $account.id; state = $account.state}
        if ($account.state -ceq 'Enabled') { $subscriptions.Add($entry) }
        else {
            $entry.reason = "Subscription state is $($account.state)"
            $excluded.Add($entry)
        }
    }
    if ($subscriptions.Count -eq 0) {
        throw "No enabled visible subscriptions in tenant $TenantId; sign in with az login --tenant $TenantId"
    }
    $context = 'reading configured cloud'
    $cloud = Invoke-AzJson -Arguments @('cloud', 'show')
    $endpoint = (Get-Text $cloud.endpoints.resourceManager).TrimEnd('/')
    if (-not $endpoint.StartsWith('https://', [StringComparison]::Ordinal)) { throw 'Expected HTTPS ARM endpoint' }
    $zones = [System.Collections.Generic.List[object]]::new()
    $vnets = [System.Collections.Generic.List[object]]::new()
    foreach ($subscription in (Sort-Resources $subscriptions.ToArray())) {
        $sub = $subscription.id
        [Console]::Error.WriteLine("Collecting subscription $sub")
        $context = "private DNS zones in subscription $sub"
        $rawZones = Get-AllPages "$endpoint/subscriptions/$sub/providers/Microsoft.Network/privateDnsZones?api-version=2018-09-01" $sub
        foreach ($raw in $rawZones) {
            $context = "zone $($raw.id) in subscription $sub"
            $zone = Get-Resource $raw 'privateDnsZones' $sub
            $path = Get-ResourcePath $zone.id
            $records = [System.Collections.Generic.List[object]]::new()
            foreach ($record in (Get-AllPages "$endpoint$path/A?api-version=2018-09-01" $sub)) {
                $entry = Get-Identity $record
                $entry.fqdn = Get-Text $record.properties.fqdn
                $ttl = $record.properties.ttl
                if ($ttl -isnot [ValueType] -or $ttl -is [bool] -or $ttl -lt 0 -or [math]::Floor($ttl) -ne $ttl) {
                    throw 'Invalid TTL'
                }
                $entry.ttl = $ttl
                $entry.isAutoRegistered = Get-Boolean $record.properties['isAutoRegistered'] -Optional
                $entry.ipv4Addresses = @(
                    if ($null -ne $record.properties['aRecords']) {
                        if ($record.properties.aRecords -isnot [array]) { throw 'Invalid aRecords array' }
                        foreach ($address in $record.properties.aRecords) { Get-Text $address.ipv4Address }
                    }
                )
                $records.Add($entry)
            }
            $links = [System.Collections.Generic.List[object]]::new()
            foreach ($link in (Get-AllPages "$endpoint$path/virtualNetworkLinks?api-version=2018-09-01" $sub)) {
                $entry = Get-Identity $link
                $entry.registrationEnabled = Get-Boolean $link.properties.registrationEnabled
                $entry.virtualNetworkLinkState = Get-OptionalText $link.properties['virtualNetworkLinkState']
                $entry.provisioningState = Get-OptionalText $link.properties['provisioningState']
                $entry.virtualNetwork = Get-VnetReference $link.properties.virtualNetwork
                $links.Add($entry)
            }
            $zone.aRecordSets = Sort-Resources $records.ToArray()
            $zone.virtualNetworkLinks = Sort-Resources $links.ToArray()
            $zones.Add($zone)
        }
        $context = "VNets in subscription $sub"
        $rawVnets = Get-AllPages "$endpoint/subscriptions/$sub/providers/Microsoft.Network/virtualNetworks?api-version=2026-03-01" $sub
        foreach ($raw in $rawVnets) {
            $context = "VNet $($raw.id) in subscription $sub"
            $vnet = Get-Resource $raw 'virtualNetworks' $sub
            $vnet.location = Get-Text $raw.location
            $path = Get-ResourcePath $vnet.id
            $peerings = [System.Collections.Generic.List[object]]::new()
            foreach ($peering in (Get-AllPages "$endpoint$path/virtualNetworkPeerings?api-version=2026-03-01" $sub)) {
                $entry = Get-Identity $peering
                $entry.peeringState = Get-OptionalText $peering.properties['peeringState']
                $entry.provisioningState = Get-OptionalText $peering.properties['provisioningState']
                foreach ($flag in @('allowVirtualNetworkAccess', 'allowForwardedTraffic', 'allowGatewayTransit', 'useRemoteGateways')) {
                    $entry[$flag] = Get-Boolean $peering.properties[$flag] -Optional
                }
                $entry.remoteVirtualNetwork = Get-VnetReference $peering.properties.remoteVirtualNetwork
                $peerings.Add($entry)
            }
            $vnet.peerings = Sort-Resources $peerings.ToArray()
            $vnets.Add($vnet)
        }
    }
    $context = 'staging inventory document'
    $linkCounts = @{}
    foreach ($zone in $zones) {
        foreach ($link in $zone.virtualNetworkLinks) {
            $id = $link.virtualNetwork.id.TrimEnd('/')
            if ($linkCounts.ContainsKey($id)) { $linkCounts[$id]++ }
            else { $linkCounts[$id] = 1 }
        }
    }
    $hubVnetId = $null
    $mostPeerings = -1
    $mostLinks = -1
    foreach ($vnet in (Sort-Resources $vnets.ToArray())) {
        $peeringCount = $vnet.peerings.Count
        $id = $vnet.id.TrimEnd('/')
        $linkCount = if ($linkCounts.ContainsKey($id)) { $linkCounts[$id] } else { 0 }
        if ($peeringCount -gt $mostPeerings -or
            ($peeringCount -eq $mostPeerings -and $linkCount -gt $mostLinks)) {
            $hubVnetId = $vnet.id
            $mostPeerings = $peeringCount
            $mostLinks = $linkCount
        }
    }
    $envelope = @{
        schemaVersion = 1; inventoryId = $inventoryId; tenantId = $TenantId.ToString()
        hubVnetId = $hubVnetId
        collectionStartedAt = $started
        collectionCompletedAt = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
        subscriptions = (Sort-Resources $subscriptions.ToArray())
        excludedSubscriptions = (Sort-Resources $excluded.ToArray())
    }
    Write-JsonFile ($envelope + @{
        privateDnsZones = (Sort-Resources $zones.ToArray())
        virtualNetworks = (Sort-Resources $vnets.ToArray())
    }) (Join-Path $stage $inventoryFile)
    $context = 'publishing inventory document'
    [IO.File]::Move((Join-Path $stage $inventoryFile), (Join-Path $OutputDirectory $inventoryFile), $true)
    [Console]::Error.WriteLine("Inventory $inventoryId written to $OutputDirectory")
} catch {
    [Console]::Error.WriteLine("Error during ${context}: $($_.Exception.Message)")
    exit 1
} finally {
    if ($stage -and [IO.Directory]::Exists($stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

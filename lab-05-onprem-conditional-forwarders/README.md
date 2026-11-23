# Lab 05 - Windows DNS conditional forwarders to central Azure DNS

[Back to the lab index](../README.md)

Discover every visible central-tenant private DNS zone linked to the selected
Azure DNS Private Resolver VNet. On the selected on-premises Windows DNS server,
create **local conditional forwarders for the corresponding public service DNS
domains**, using the central resolver's inbound IP addresses. Custom zones without
a Private Link label retain their original names.

Supply the inbound endpoint's **IPv4 address(es) directly**, using `-InboundIp`
in PowerShell or `--inbound-ip` in the exporter. Multiple addresses are preserved
in the order provided. Do not supply an ARM endpoint ID or Azure's `168.63.129.16`
platform address as an on-premises DNS target.

The script validates usable IPv4 addresses, but **does not discover endpoints or
verify that the supplied IPs belong to the resolver**. Confirm the correct IPs
and test connectivity before applying.

## Scope and safeguards

- Azure discovery is read-only and live, not based on the Lab 01 zone snapshot.
  It covers Enabled subscriptions visible in the Azure CLI account list for the
  explicit central tenant and follows pagination for zones and VNet links.
- Only zones whose healthy, completed link points to the **exact selected
  resolver VNet** are included. Links to hubs, spokes, other resolvers, or
  non-central zones do not qualify.
- The PowerShell script maps these private-zone names to public forwarding
  domains before checking or creating Windows rules. Shared forwarding domains
  are deduplicated. Azure private-zone names, links, and records remain unchanged.
- The resolver VNet, supplied IP format, subscription ownership,
  and selected zone/link health are checked. Discovery errors stop the export;
  they are not converted into an empty successful result.
- Newly created Windows forwarders are **local**, not AD-integrated. No domain/forest
  replication is enabled.
- All selected namespaces are preflighted before any DNS write. An existing
  exact-name authoritative or other non-forwarder zone remains a conflict.
  The script stops; it does not overwrite, delete, or convert the zone.
- **Any existing conditional forwarder for a selected public forwarding domain is skipped**,
  regardless of its IPs/order, AD integration, paused/shutdown state, or recursion
  settings. Missing rules continue to be created. Existing settings are not
  checked for conformity or changed.
- New forwarders use the supplied IP order, a five-second timeout, local storage,
  and no recursion fallback. A forwarder appearing after preview is also skipped.
- A sorted **list of skipped domain rules** is printed at the end, including
  during `-WhatIf` and after failures. If none were encountered, it prints `None`.
- Existing `privatelink.*` Windows forwarders are not renamed or deleted. They
  do not count as an existing rule for the corresponding public domain.
- Unrelated Windows zones, records, server-wide forwarders, root hints, firewall
  settings, Azure resources, and Azure forwarding rulesets are not changed.
- A zone removed from a later Azure discovery is not automatically removed from
  Windows DNS. No destructive reconciliation or automatic rollback is performed.

## Prerequisites

For Azure discovery:

- The complete repository checkout, Python 3.10+, and Azure CLI.
  [export_zones.py](./scripts/export_zones.py) reuses Lab 04's VNet validation
  and the Lab 03/01/02 validation/Azure helpers.
- An authenticated human in the central tenant, with read access to its resolver
  VNet and private DNS zones/links across every intended subscription.
  No service principal or managed identity is created.
- Sign in explicitly and refresh the account cache before collecting:

  ```bash
  az login --tenant "$TENANT_A_ID"
  az account list --all --refresh --output table
  ```

Discovery cannot enumerate subscriptions or zones hidden by RBAC. Its manifest
records the covered subscription IDs: review these for completeness. The absence
of inaccessible resources is not proof that none exist.

For Windows DNS configuration:

- Windows Server with the DNS Server role already installed, or a Windows
  management host with the `DnsServer` PowerShell module/RSAT DNS tools.
- Windows PowerShell 5.1+, or PowerShell 7 on Windows with a working `DnsServer`
  module. DNS cmdlets must expose the conditional forwarder's configuration.
- Current Windows credentials with permission to read/create zones on the
  specified DNS server. Azure credentials do not grant Windows DNS permissions.
- Working on-premises-to-Azure routing (for example, VPN/ExpressRoute), with
  **UDP and TCP 53** allowed from the Windows DNS server to every inbound IP.
- Correct records already present in the central private zones. This lab exports
  zone names/links, not record data, and does not migrate records.

The script does not install server roles, management tools, or networking.

## Private-zone to public-domain mapping

For Azure Private Endpoints, on-premises DNS must forward the application's
public service namespace to Azure so the public-to-private CNAME chain is
resolved there. Do not blindly remove `privatelink.`: some services require
different, multiple, or regional public forwarding domains.

Examples of built-in mappings:

| Linked Azure private zone | Windows conditional-forwarding domain(s) |
| --- | --- |
| `privatelink.blob.core.windows.net` | `blob.core.windows.net` |
| `privatelink.file.core.windows.net` | `file.core.windows.net` |
| `privatelink.database.windows.net` | `database.windows.net` |
| `privatelink.vaultcore.azure.net` | `vault.azure.net`, `vaultcore.azure.net` |
| `privatelink.azurewebsites.net` | `azurewebsites.net`, `scm.azurewebsites.net` |
| `privatelink.ts.eventgrid.azure.net` | `eventgrid.azure.net` |
| `privatelink.wvd.microsoft.com`, `privatelink-global.wvd.microsoft.com` | `wvd.microsoft.com` |
| `eastus.data.privatelink.azurecr.io` | `eastus.data.azurecr.io` |
| `cluster.privatelink.westeurope.azmk8s.io` | `westeurope.azmk8s.io` |
| `privatelink.westeurope.azurecontainerapps.io` | `azurecontainerapps.io` |

Other built-in mappings cover classic Storage account blob/file/DFS/queue/table/web,
SQL Database, Service Bus, common Cosmos DB, PostgreSQL/MySQL, ACR, App Configuration,
Managed HSM, classic Redis, Search, and Foundry endpoint suffixes. The script prints
every source-to-target mapping before the Windows DNS preview.

Unknown Private Link namespaces stop before Windows DNS reads/writes rather than
guessing a public domain. Supply explicit `-ForwardingDomainMap` overrides for
unsupported or service-specific cases. Keys are linked private-zone names; values
are one public domain string or an array of public domains. For example, an
East US Automation endpoint needs an explicit regional mapping:

```powershell
.\Setup-ConditionalForwarders.ps1 `
  -InputFile .\lab05-conditional-forwarders.json `
  -DnsServer "dns01.corp.example" `
  -ForwardingDomainMap @{
    'privatelink.azure-automation.net' = @('eus.azure-automation.net')
  } `
  -WhatIf
```

Overrides work in both live and file modes and take precedence over built-ins.
Validate the correct service/region against Microsoft's
[public DNS zone forwarder table](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns).
Some services share a private-zone name: the default Storage mappings above are
for classic Storage accounts. Elastic SAN can require `blob.storage.azure.net`,
and `Microsoft.FileShares/fileShares` can require `file.storage.azure.net`; supply
an override if those services use the same discovered private zone.

Forwarding public domains sends all queries in those namespaces through Azure,
including queries for public resources. Review that broader routing scope and
any public-resource NXDOMAIN/fallback requirements before applying.

## Option A - Live collection and Windows DNS setup

Run [Setup-ConditionalForwarders.ps1](./scripts/Setup-ConditionalForwarders.ps1)
on a Windows host that has both Azure CLI/Python and the DNS management module.
Use the central resolver VNet ARM ID from the central entry in the Lab 04 map,
and supply that resolver's inbound IPv4 address(es).

Preview from the repository root:

```powershell
.\lab-05-onprem-conditional-forwarders\scripts\Setup-ConditionalForwarders.ps1 `
  -CentralTenantId "$TENANT_A_ID" `
  -ResolverVnetId "<full-central-resolver-vnet-arm-id>" `
  -InboundIp "10.10.1.4" `
  -DnsServer "dns01.corp.example" `
  -WhatIf
```

For multiple targets, use `-InboundIp "10.10.1.4","10.10.1.5"`.
This replaces the previous `-InboundEndpointId` parameter.

`-WhatIf` reads Azure and Windows DNS but creates no forwarders or manifest files.
Review the private-to-public mappings, forwarding namespaces, inbound IPs,
DNS server name, and any conflicts.
Remove `-WhatIf` to apply; PowerShell asks for confirmation before each addition.
`-Confirm:$false` explicitly suppresses these prompts for an already reviewed run.
Omit `-DnsServer` to use the local Windows computer.

The script calls the shared Python discovery helper, then creates and reads back
each new forwarder to verify its type, local storage, active state, target IPs,
IP order, and disabled recursion fallback. Creation errors or verification
mismatches stop subsequent additions and report that partial changes remain.
Existing conditional forwarders are not verified against the new targets. Review
the final skipped list separately if those rules must also point to the central
resolver:

```text
Skipped existing conditional forwarders (2):
  blob.core.windows.net
  file.core.windows.net
```

## Option B - Export in Azure, configure on a separate Windows host

If Python/Azure CLI cannot be installed on the Windows DNS management host,
export a manifest from your Azure management workstation (Linux is supported):

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  lab-05-onprem-conditional-forwarders/scripts/export_zones.py export \
  --central-tenant-id "$TENANT_A_ID" \
  --resolver-vnet-id "<full-central-resolver-vnet-arm-id>" \
  --inbound-ip 10.10.1.4 \
  --output-file ./.inventory/lab05-conditional-forwarders.json
```

For multiple targets, use `--inbound-ip 10.10.1.4 10.10.1.5`.
This replaces the previous `--inbound-endpoint-id` parameter.

The default output is `./.inventory/lab05-conditional-forwarders.json`.
The file is written atomically with owner-only permissions, only after the
complete collection succeeds. A failed collection leaves a previous file intact.
The `discover` action instead prints the manifest as JSON without saving it.

Example manifest:

```json
{
  "schemaVersion": 2,
  "generatedAt": "2026-11-23T21:00:00+00:00",
  "centralTenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "resolverVnetId": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver",
  "subscriptionIds": ["11111111-1111-1111-1111-111111111111"],
  "dnsServers": ["10.10.1.4"],
  "zones": [
    {
      "name": "privatelink.blob.core.windows.net",
      "id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/dns/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net",
      "subscriptionId": "11111111-1111-1111-1111-111111111111"
    }
  ]
}
```

New exports use schema version 2 and omit `inboundEndpointId`. Existing version 1
manifests remain supported in file mode; their endpoint ID is validated as
metadata, while their saved `dnsServers` remain the forwarding targets.
The manifest continues to contain the original private-zone names and ARM IDs.
Public-domain conversion happens only in PowerShell; existing exports do not
need a schema change or renaming to use these mappings.

Transfer the reviewed manifest and the standalone PowerShell setup script to
the Windows management host using your approved process. File mode needs neither
Python nor Azure CLI, and makes no Azure requests:

```powershell
.\Setup-ConditionalForwarders.ps1 `
  -InputFile .\lab05-conditional-forwarders.json `
  -DnsServer "dns01.corp.example" `
  -WhatIf
```

Review, then run again without `-WhatIf`. The manifest is a snapshot, not a live
Azure check: review its timestamp and regenerate it if links or endpoint IPs
change. Avoid concurrent DNS/endpoint changes during either workflow. An empty
zone list is a no-op, not an instruction to remove existing forwarders.

## Verify resolution

Inspect a configured forwarder:

```powershell
Get-DnsServerZone -ComputerName "dns01.corp.example" `
  -Name "blob.core.windows.net" |
  Format-List ZoneName, ZoneType, IsDsIntegrated, MasterServers, UseRecursion
```

Use a known existing storage account's public hostname to test the inbound endpoint directly and then the
normal Windows DNS path, over both UDP and TCP:

```powershell
Resolve-DnsName -Name "<storage-account>.blob.core.windows.net" `
  -Server "10.10.1.4" -DnsOnly
Resolve-DnsName -Name "<storage-account>.blob.core.windows.net" `
  -Server "10.10.1.4" -DnsOnly -TcpOnly
Resolve-DnsName -Name "<storage-account>.blob.core.windows.net" `
  -Server "dns01.corp.example" -DnsOnly
```

Test from a representative on-premises client too. Successful configuration is
not proof of DNS reachability; cached responses can conceal forwarding problems.

Confirm that the public hostname's CNAME chain ends at the expected private IP.
Also test the private alias directly when troubleshooting, but public forwarding
domains are the Windows rules this lab now creates. Review skipped public rules
and any old, more-specific `privatelink.*` rules separately; they remain untouched.

## Recovery

Preserve the creation output after a partial failure. Earlier successful
additions remain; rerunning skips all existing conditional forwarders and attempts
only missing ones. Resolve conflicts manually rather than overwriting authoritative zones.

To undo an addition, first verify the exact named zone is the intended **local
conditional forwarder** with the expected IPs. Then remove only that reviewed
zone:

```powershell
Get-DnsServerZone -ComputerName "dns01.corp.example" -Name "<reviewed-zone-name>" |
  Format-List ZoneName, ZoneType, IsDsIntegrated, MasterServers
Remove-DnsServerZone -ComputerName "dns01.corp.example" -Name "<reviewed-zone-name>"
```

`Remove-DnsServerZone` can delete authoritative zones as well; never bulk-delete
zones from a manifest or remove a zone merely because discovery no longer lists
it. Removing an AD-integrated zone also replicates its deletion.

## Local validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab-05-onprem-conditional-forwarders/tests -v
```

```powershell
powershell.exe -NoProfile -File `
  .\lab-05-onprem-conditional-forwarders\tests\Test-ConditionalForwarders.ps1
# Alternatively, use pwsh -NoProfile -File with the same test path.
```

The standard-library Python tests mock Azure APIs and check coverage, pagination,
exact resolver links, failures, and manifest persistence. The dependency-free
PowerShell tests mock DNS cmdlets and verify previews, local conditional creation,
public-domain mappings/overrides, deduplication, non-forwarder conflicts,
skipped-rule reporting, input validation, freshness, and partial failures.
They do not validate a live Windows DNS service or actual Azure DNS resolution.

References: [Windows Server DNS forwarding](https://learn.microsoft.com/en-us/windows-server/networking/dns/configure-dns-forwarding)
and [Add-DnsServerConditionalForwarderZone](https://learn.microsoft.com/en-us/powershell/module/dnsserver/add-dnsserverconditionalforwarderzone).

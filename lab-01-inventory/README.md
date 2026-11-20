# Lab 01 - Private DNS and VNet inventory

Read-only, tenant-scoped Azure inventory with an offline hub-linked DNS report.
No deployment or Azure resource changes are performed.

## Requirements and permissions

| Component | Supported baseline |
| --- | --- |
| Azure CLI | 2.61.0 or later, authenticated before export |
| Bash exporter | Bash 4.4 or later, jq 1.6 or later, standard Unix utilities |
| PowerShell exporter | PowerShell 7.4.0 or later; no Az modules or jq required |
| Offline report/tests | Python 3.10.0 or later, standard library only |

Bash uses `date`, `dirname`, `mkdir`, `mktemp`, `mv`, `rm`, `sleep`, `grep`, and
`cat`. Windows users can use the PowerShell exporter and `python` instead of
`python3` for the report. The optional exporter integration tests use a Unix
mock executable; run those on Linux/macOS (Bash 4.4+), or WSL with Linux PowerShell.

Use an identity with **Reader** or equivalent management-plane access over
every intended subscription, including:

- Subscription/resource enumeration.
- `Microsoft.Network/privateDnsZones/read`,
  `Microsoft.Network/privateDnsZones/A/read`, and
  `Microsoft.Network/privateDnsZones/virtualNetworkLinks/read`.
- `Microsoft.Network/virtualNetworks/read` and
  `Microsoft.Network/virtualNetworks/virtualNetworkPeerings/read`.

These are management-plane reads; no DNS queries, VM access, or data-plane
permissions are required. No cross-tenant sign-in is performed to resolve remote
VNet references. Exported JSON contains infrastructure names, IDs, and private IP
addresses: protect it appropriately and do not commit real inventories.

## Export

Run from the repository root. Replace the example tenant GUID with your tenant.
Sign in explicitly; neither script initiates interactive authentication.

### Bash

```bash
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
bash lab-01-inventory/scripts/export-inventory.sh \
  --tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --output-directory "./.inventory"
```

Keep the adjacent [jq helper](./scripts/inventory.jq) with the Bash script.

### PowerShell

```powershell
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
pwsh -NoProfile -File ./lab-01-inventory/scripts/Export-Inventory.ps1 `
  -TenantId aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  -OutputDirectory "./.inventory"
```

Either alternative writes one file, `<output-directory>/<tenantId>-inventory.json`,
containing both:

- `privateDnsZones`: all visible private zones, A record sets
  (including auto-registration and apex `@`), and VNet links.
- `virtualNetworks`: all visible VNets and their local peering
  resources.

`<tenantId>` is the requested tenant GUID. Exports for different tenants can
share a directory without overwriting each other's files. Existing separate
DNS/VNet files (prefixed or unprefixed) are left untouched; they are not renamed
or updated.

Both use subscription-wide lists, so resource groups do not need to be supplied.
Explicit child-resource list calls follow every REST `nextLink`, including for
A records, links, and peerings. Pagination cycles, unexpected endpoint URLs,
malformed responses, and Azure CLI failures stop collection with nonzero status
and contextual diagnostics on stderr. Only recognized transient HTTP errors
(429/500/502/503/504 and corresponding error codes) get up to three attempts,
with one- then two-second backoff. Authorization failures are not retried.

The scripts refresh the CLI subscription cache with
`az account list --all --refresh`, filter to the requested tenant, and collect
all discovered `Enabled` subscriptions. Other states in that tenant appear in
`excludedSubscriptions` with reasons. Other tenants are outside the requested
scope. No enabled subscriptions is an error. Resource calls always specify
`--subscription`; scripts never select a new default subscription or change
cloud/configuration. The refresh itself updates Azure CLI's local account cache.

**Visibility limitation:** "all" means resources visible to the signed-in
identity, not independently verified tenant-wide completeness. An inaccessible
subscription may never be returned, and partial RBAC visibility may be
undetectable. Verify coverage against your organization's subscription register.
An explicit denial fails the export rather than silently skipping resources.

The combined document is staged in a private, uniquely named directory inside
the destination and published with one file replacement after both collections
are complete. Collection/parsing errors leave the existing inventory intact and
remove that staging directory. Do not run simultaneous exports for the same
tenant to the same destination.

Collection is not an atomic Azure snapshot: resources can change between calls.

## JSON schema (version 1)

The [legacy DNS fixture](./tests/fixtures/private-dns-zones.json) and
[legacy VNet fixture](./tests/fixtures/virtual-networks.json) illustrate the
collections. The combined format contains both arrays under one metadata envelope.

The inventory document has:

| Field | Type/meaning |
| --- | --- |
| `schemaVersion` | Integer `1` |
| `inventoryId` | Generated string identifying this export |
| `tenantId` | Requested tenant GUID |
| `hubVnetId` | Automatically selected hub VNet ARM ID, or `null` when no VNets exist |
| `collectionStartedAt`, `collectionCompletedAt` | ISO 8601 UTC strings |
| `subscriptions` | Array of `{name, id, state}` for included subscriptions |
| `excludedSubscriptions` | Array of `{name, id, state, reason}` for excluded states |
| `privateDnsZones` | Private DNS zone array |
| `virtualNetworks` | VNet array |

Hub selection considers VNets across all collected subscriptions. It chooses
the VNet with the most local peering resources, regardless of peering state.
Ties are resolved by the most private DNS zone links referencing the VNet
(across all collected zones), then the lowest case-insensitive VNet ID.
Link references are compared case-insensitively and ignore trailing slashes.
Even if all VNets have zero peerings, these tie-breakers select one VNet.
This is an inventory-based heuristic, not proof of the intended hub topology.

Every resource and VNet reference has `name` and full ARM resource `id`.
Subscription objects use GUID IDs. Resource identities are IDs, not names.
Original ID spelling is preserved. All resource collections, including children
and coverage, are sorted case-insensitively by ID. Empty and single-element
collections remain arrays.

- **Zone:** `name`, `id`, `subscriptionId`, `resourceGroup`, `aRecordSets`,
  `virtualNetworkLinks`.
- **A record set:** `name`, `id`, `fqdn`, integer `ttl`, nullable boolean
  `isAutoRegistered`, and `ipv4Addresses` (array of strings). An absent/null
  service `aRecords` collection is represented as an empty address array.
  IP addresses are values, not separate ARM resources.
- **VNet link:** `name`, `id`, boolean `registrationEnabled`, nullable string
  `virtualNetworkLinkState` and `provisioningState`, and `virtualNetwork`
  (`{name,id}`).
- **VNet:** `name`, `id`, `subscriptionId`, `resourceGroup`, `location`,
  `peerings`.
- **Peering:** `name`, `id`, nullable strings `peeringState` and
  `provisioningState`, nullable booleans `allowVirtualNetworkAccess`,
  `allowForwardedTraffic`, `allowGatewayTransit`, `useRemoteGateways`, and
  `remoteVirtualNetwork` (`{name,id}`).

Nullable fields represent properties not supplied by Azure, never failed
enumerations. Remote VNet names are derived from the last segment of their ARM
ID without additional Azure queries.

## Generate the offline CSV

Obtain the hub's **full ARM ID**, for example from `hubVnetId` in the inventory
(confirm the automatically selected VNet is your intended hub), or
explicitly running this optional operator command:

```bash
az network vnet show --subscription 11111111-1111-1111-1111-111111111111 \
  --resource-group network --name hub --query id --output tsv
```

Then run (no Azure CLI, credentials, or network access required):

```bash
python3 lab-01-inventory/scripts/report_dns_hub_links.py \
  --inventory-json "./.inventory/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-inventory.json" \
  --hub-vnet-id "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/hub" \
  --output-csv "./hub-dns-links.csv"
```

PowerShell can run the same Python script, replacing Bash's line-continuation
backslashes with backticks (and `python3` with `python` when appropriate).
The CSV's parent directory must already exist.

The script validates schema version, required fields/types, ARM identities,
subscription coverage, and timestamps for both collections. The
hub must exist in the VNet inventory. ID comparisons ignore case and trailing
slashes. Invalid input returns nonzero and preserves an existing CSV.

Legacy two-file inventories remain supported: use `--private-dns-json <dns-file>`
and `--vnets-json <vnet-file>` instead of `--inventory-json`. The report checks
that their metadata agrees. Do not mix the combined and legacy input options.

A VNet is eligible if it is the hub or a **direct** peer recorded at either
endpoint. A one-way peering entry is sufficient evidence. Direct remote peers
outside the inventory/tenant qualify when a recorded peering establishes the
relationship. All recorded peering states, including `Initiated` and
`Disconnected`, are included. Peer-of-a-peer VNets are not included.

There is **one row per distinct matching zone/link pair**, not one row per zone.
A zone linked to both hub and spoke has two rows. Duplicate evidence is removed
by zone/link IDs, while duplicate names with different IDs remain distinct.
Conflicting duplicate evidence is an error. No matches produces a header-only
CSV. CSV quoting supports commas, quotes, and embedded newlines.

Columns, in order:

```text
privateDnsZoneName,privateDnsZoneId,privateDnsZoneSubscriptionId,privateDnsZoneResourceGroup,virtualNetworkLinkName,virtualNetworkLinkId,linkedVnetName,linkedVnetId,relationshipToHub,hubVnetName,hubVnetId,tenantId
```

`relationshipToHub` is `hub` or `directPeer`. See the
[expected fixture CSV](./tests/fixtures/expected.csv).
`tenantId` is the inventory tenant GUID on every row, including links to external
peers; it does not identify the remote VNet's tenant.

**This is relationship inventory, not connectivity validation.** Peering does
not make private DNS zone links transitive. The report includes only explicit
links to eligible VNets, not zones automatically inherited by peers, and proves
neither DNS resolution nor data-plane reachability.

## Consolidate hub-link CSVs into a central-tenant plan

From the repository root, select the tenant that will host the private DNS zones.
One command generates all tenant hub-link reports and the consolidated plan:

```bash
bash lab-01-inventory/scripts/consolidate-dns-links.sh \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
```

The default directory is `./inventory`. To use another directory, add
`--inventory-directory "./.inventory"`. The script first calls the batch
reporting implementation, then consolidates only the CSVs successfully generated
in that run, in inventory filename order. Old CSVs belonging to skipped tenants
or tenants without a current JSON inventory are ignored.

Requires Bash and Python 3 (standard library only). Keep the adjacent report
scripts together. The generated `<tenantId>-hub-dns-links.csv` files and the final
`hub-dns-zones.csv` are all written in the inventory directory. Explicit input
CSV and output path arguments are no longer used.
The final output columns, in order, are:

```text
privateDnsZoneName,tenantId,privateDnsZoneId,privateDnsZoneSubscriptionId,privateDnsZoneResourceGroup,centralTenantOperation,originalTenantOperation
```

One row is retained per tenant/zone ID, removing repeated hub/spoke link rows.
The same domain with different zone IDs remains separate. The first occurrence
and its original metadata spelling are preserved; conflicting metadata for the
same tenant/zone ID is an error. Tenant/zone ID comparisons ignore case, and zone
IDs ignore trailing slashes. Domain comparisons ignore case and trailing dots.

All central-tenant zones are considered before assigning operations, regardless
of input order:

| Zone | `centralTenantOperation` | `originalTenantOperation` |
| --- | --- | --- |
| Already in the central tenant | `none` | `none` |
| Other tenant, domain already in central or planned for creation | `repeated` | `delete` |
| Other tenant, domain not yet in central or planned | `create` | `delete` |

A missing domain is marked `create` only on its first non-central occurrence;
subsequent occurrences are `repeated`. The tenant ID, subscription ID, resource
group, and zone ID always describe the **original** zone, not its future central
destination.

**No Azure resources are created or deleted.** This CSV is a plan, not an
execution script. Existence checks cover only the generated CSVs; hub-link
reports omit zones without eligible links. Ensure the inputs cover all relevant
central zones before treating a `create` operation as authoritative. A `delete`
label is not proof that DNS records, links, or dependencies have been migrated.

Quoted CSV fields and header-only inputs are supported. Required metadata must
be nonempty and tenant IDs must be GUIDs. The inventory directory must already
exist and contain matching tenant JSON files. A batch-reporting failure stops
consolidation and preserves the existing final CSV. Reports already generated
before a later failure remain in place. An existing final output is replaced
only after successful consolidation. If every tenant is skipped or has no
matching links, the final CSV contains only its header.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab-01-inventory/tests -p 'test_inventory.py' -v
bash -n lab-01-inventory/scripts/export-inventory.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab-01-inventory/tests -p 'test_consolidate_dns_links.py' -v
bash -n lab-01-inventory/scripts/consolidate-dns-links.sh
```

Tests replace `az` with a strict local mock and never invoke the real Azure CLI.
They cover schema-equivalent exports, multiple subscriptions, every paginated
collection, empty/singleton results, contextual failures, preservation of old
outputs, bounded retry, direct/incoming/external relationships, non-transitivity,
duplicates, ID normalization, escaping, invalid input, and batch reporting with
per-tenant hub IDs and output filenames.

PowerShell tests are skipped if `pwsh` is unavailable. Set `PWSH` to an explicit
PowerShell executable to include them. Azure live integration and Windows-native
execution require separate operator validation; these tests do not establish
real tenant permissions or service availability.

## API references

The exporters use Azure CLI `az rest --method get` with the configured ARM
endpoint. The pinned stable API versions are Private DNS `2018-09-01` and Virtual
Networks `2026-03-01`, following Microsoft's published contracts:

- [Subscription discovery and refresh](https://learn.microsoft.com/en-us/cli/azure/account#az-account-list)
- [Azure CLI REST](https://learn.microsoft.com/en-us/cli/azure/reference-index#az-rest)
- [Private zones list](https://learn.microsoft.com/en-us/rest/api/dns/privatedns/private-zones/list?view=rest-dns-privatedns-2018-09-01)
- [A record sets list by type](https://learn.microsoft.com/en-us/rest/api/dns/privatedns/record-sets/list-by-type?view=rest-dns-privatedns-2018-09-01)
- [Private DNS VNet links list](https://learn.microsoft.com/en-us/rest/api/dns/privatedns/virtual-network-links/list?view=rest-dns-privatedns-2018-09-01)
- [VNets list all](https://learn.microsoft.com/en-us/rest/api/virtualnetwork/virtual-networks/list-all?view=rest-virtualnetwork-2026-03-01)
- [VNet peerings list](https://learn.microsoft.com/en-us/rest/api/virtualnetwork/virtual-network-peerings/list?view=rest-virtualnetwork-2026-03-01)

API availability in sovereign clouds may differ; those environments are not
validated by this lab. An unsupported API version fails explicitly.

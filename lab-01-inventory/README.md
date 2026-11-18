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
  --output-directory "./inventory"
```

Keep the adjacent [jq helper](./scripts/inventory.jq) with the Bash script.

### PowerShell

```powershell
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
pwsh -NoProfile -File ./lab-01-inventory/scripts/Export-Inventory.ps1 `
  -TenantId aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  -OutputDirectory "./inventory"
```

Either alternative writes:

- `inventory/private-dns-zones.json`: all visible private zones, A record sets
  (including auto-registration and apex `@`), and VNet links.
- `inventory/virtual-networks.json`: all visible VNets and their local peering
  resources.

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

Files are staged in a private, uniquely named directory inside the destination.
Collection/parsing errors leave existing files intact and remove that staging
directory. Both documents are prepared before publication, but their two file
replacements are **not a transaction**: an interrupted publication can leave
mismatched files. The report rejects different inventory IDs. Rerun the export
into a separate directory after such an interruption. Do not run simultaneous
exports to the same destination.

Collection is not an atomic Azure snapshot: resources can change between calls.

## JSON schema (version 1)

The [DNS fixture](./tests/fixtures/private-dns-zones.json) and
[VNet fixture](./tests/fixtures/virtual-networks.json) are complete examples.

Each document has:

| Field | Type/meaning |
| --- | --- |
| `schemaVersion` | Integer `1` |
| `inventoryId` | Shared generated string for this pair |
| `tenantId` | Requested tenant GUID |
| `collectionStartedAt`, `collectionCompletedAt` | Shared ISO 8601 UTC strings |
| `subscriptions` | Array of `{name, id, state}` for included subscriptions |
| `excludedSubscriptions` | Array of `{name, id, state, reason}` for excluded states |
| `privateDnsZones` or `virtualNetworks` | Resource array appropriate to the file |

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

Obtain the hub's **full ARM ID**, for example by inspecting the VNet JSON, or
explicitly running this optional operator command:

```bash
az network vnet show --subscription 11111111-1111-1111-1111-111111111111 \
  --resource-group network --name hub --query id --output tsv
```

Then run (no Azure CLI, credentials, or network access required):

```bash
python3 lab-01-inventory/scripts/report_dns_hub_links.py \
  --private-dns-json "./inventory/private-dns-zones.json" \
  --vnets-json "./inventory/virtual-networks.json" \
  --hub-vnet-id "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/hub" \
  --output-csv "./hub-dns-links.csv"
```

PowerShell can run the same Python script, replacing Bash's line-continuation
backslashes with backticks (and `python3` with `python` when appropriate).
The CSV's parent directory must already exist.

The script validates schema version, required fields/types, ARM identities,
subscription coverage, timestamps, and agreement between both documents. The
hub must exist in the VNet inventory. ID comparisons ignore case and trailing
slashes. Invalid input returns nonzero and preserves an existing CSV.

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
privateDnsZoneName,privateDnsZoneId,privateDnsZoneSubscriptionId,privateDnsZoneResourceGroup,virtualNetworkLinkName,virtualNetworkLinkId,linkedVnetName,linkedVnetId,relationshipToHub,hubVnetName,hubVnetId
```

`relationshipToHub` is `hub` or `directPeer`. See the
[expected fixture CSV](./tests/fixtures/expected.csv).

**This is relationship inventory, not connectivity validation.** Peering does
not make private DNS zone links transitive. The report includes only explicit
links to eligible VNets, not zones automatically inherited by peers, and proves
neither DNS resolution nor data-plane reachability.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab-01-inventory/tests -p 'test_inventory.py' -v
bash -n lab-01-inventory/scripts/export-inventory.sh
```

Tests replace `az` with a strict local mock and never invoke the real Azure CLI.
They cover schema-equivalent exports, multiple subscriptions, every paginated
collection, empty/singleton results, contextual failures, preservation of old
outputs, bounded retry, direct/incoming/external relationships, non-transitivity,
duplicates, ID normalization, escaping, and invalid input.

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

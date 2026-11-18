# Azure private DNS and VNet inventory generation prompt

## Copy-ready prompt

Act as a senior Azure networking engineer. Implement a read-only Azure inventory
and offline reporting solution. Generate code, focused tests, and documentation;
do not sign in, query a live tenant, deploy, or modify Azure resources.

### Deliverables

Create a new `lab-01-inventory/` folder, following the repository's lab layout:

- `scripts/export-inventory.sh`: Bash implementation using Azure CLI and jq.
- `scripts/Export-Inventory.ps1`: PowerShell 7 implementation using Azure CLI
  and native PowerShell JSON handling, not the Az PowerShell modules.
- `scripts/report_dns_hub_links.py`: one additional, cross-platform Python 3
  script using only the standard library, consuming the two JSON files offline
  and producing a CSV.
- Focused tests and a README with prerequisites, permissions, schemas, limitations,
  and copy-ready examples for both exporters and the report.
- Add the new lab to the root README's lab table without changing other labs.

The Bash and PowerShell scripts are interchangeable alternatives. Each execution
must produce the same two JSON document schemas:
`private-dns-zones.json` and `virtual-networks.json`.

### Exporter inputs and scope

- Require an explicit tenant ID and accept an output-directory parameter.
- Use an existing Azure CLI authentication session. Document
  `az login --tenant <tenant-id>` as an operator prerequisite; never initiate
  interactive login automatically or embed credentials.
- Refresh/discover subscriptions available to the signed-in identity, restrict
  them to the requested tenant, and inventory every enabled subscription in that
  tenant. Record disabled or otherwise excluded discovered subscriptions and
  the reason. Fail clearly when no enabled subscriptions are available.
- Azure CLI can discover only subscriptions/resources visible to the identity.
  Do not claim tenant-wide completeness beyond that access boundary. Document
  the Reader-equivalent management-plane permissions required across all intended
  subscriptions and that partial RBAC visibility may be undetectable.
- Pass `--subscription` explicitly to resource queries. Do not change the default
  subscription, cloud, tenant, or persistent Azure CLI configuration.
- Discover all resource groups automatically. Support resources spread across
  subscriptions and resource groups, duplicate resource names, and empty results.
- Inventory ARM private DNS zones, not public DNS zones, Private Resolver
  forwarding rulesets, or private endpoint DNS zone groups.
- Use supported Azure CLI commands. Verify command names, arguments, response
  shapes, and pagination behavior against current Microsoft documentation when
  implementing. Retrieve every page, including child-resource collections.

### JSON contract

Use UTF-8 JSON with a documented, versioned schema. Both files from one run must
share a generated `inventoryId`, tenant ID, collection start/end UTC timestamps,
and subscription coverage metadata. Use these envelope fields:

```json
{
  "schemaVersion": 1,
  "inventoryId": "<shared run identifier>",
  "tenantId": "<requested tenant ID>",
  "collectionStartedAt": "<ISO 8601 UTC>",
  "collectionCompletedAt": "<ISO 8601 UTC>",
  "subscriptions": [
    {"name": "<subscription name>", "id": "<subscription GUID>", "state": "Enabled"}
  ],
  "excludedSubscriptions": [],
  "privateDnsZones": []
}
```

In the second document replace `privateDnsZones` with `virtualNetworks`; keep
the other envelope fields identical. Each excluded-subscription entry must
include its name, subscription GUID, state, and exclusion reason.

Every Azure resource object and resource reference must contain its actual
`name` and full ARM resource `id`. Subscription IDs remain GUIDs, as shown above.
Never use a name alone as an identity. Preserve original IDs in output.

For each entry in `privateDnsZones`, include:

- Zone `name`, `id`, `subscriptionId`, and `resourceGroup`.
- `aRecordSets`: every A record set, including auto-registered records and `@`
  when present. Each entry contains the record-set resource `name` and `id`,
  `fqdn`, `ttl`, and an `ipv4Addresses` array containing every address in the set.
  An individual IP address is a value, not a separately identified ARM resource.
- `virtualNetworkLinks`: every link, containing its own `name` and `id`,
  `registrationEnabled`, available link/provisioning states, and a
  `virtualNetwork` reference with `name` and `id`.

For each entry in `virtualNetworks`, include:

- VNet `name`, `id`, `subscriptionId`, `resourceGroup`, and `location`.
- `peerings`: all local peering resources, each with its own `name` and `id`,
  `peeringState`, `provisioningState`, `allowVirtualNetworkAccess`,
  `allowForwardedTraffic`, `allowGatewayTransit`, `useRemoteGateways`, and a
  `remoteVirtualNetwork` reference containing `name` and `id`.

Keep all collections as arrays, including zero/one-element collections. Use JSON
booleans and numbers, not strings. Document nullable service properties. Obtain
referenced VNet names from their ARM IDs when necessary without querying a
possibly inaccessible remote tenant. Do not fabricate resource IDs.
Sort collections deterministically by resource ID.

### Reliability and operational behavior

- Keep all Azure operations read-only. Check external-command exit codes in
  both languages; PowerShell error preferences alone do not ensure `az` failures
  terminate the script. Use sufficient JSON serialization depth in PowerShell.
- Fail with actionable subscription/resource/operation context on authentication,
  authorization, command, parsing, or enumeration failure. Do not silently skip a
  failed subscription/resource or turn an error into an empty collection.
- An actual empty successful response is valid. A failed child query is not.
- Use bounded retry/backoff only for recognized transient failures. Do not retry
  authorization or invalid-input failures indefinitely.
- Validate dependencies and inputs before collection. Quote arguments safely,
  support paths containing spaces, and keep progress/diagnostics separate from
  serialized output.
- Stage both documents and publish them only after collection and validation
  succeed. Clean up this run's temporary files on failure without deleting
  unrelated files or replacing valid output with partial data. Use the shared
  inventory ID to detect mismatched files; do not claim two file replacements
  constitute a transactional operation.
- Document that collection is not an atomic Azure snapshot: resources and links
  can change during enumeration.

### Offline hub-linked DNS report

The Python script must require `--private-dns-json`, `--vnets-json`,
`--hub-vnet-id`, and `--output-csv`.

1. Validate both JSON documents, required fields and types, supported schema
   versions, and matching tenant IDs, inventory IDs, and coverage metadata.
   Reject malformed, mismatched, or unsupported input with a clear error and
   nonzero exit status.
2. Require the hub's full VNet ARM ID, not its name, and verify that the hub
   exists in the VNet inventory. Compare ARM IDs case-insensitively and normalize
   trailing slashes consistently, while preserving original values for output.
3. Build the eligible set from the hub itself and its directly peered VNets.
   Treat a recorded peering with the hub at either endpoint as evidence of a
   direct relationship, even if its reverse peering or remote VNet inventory is
   absent. Include remote VNet references outside the exported tenant when a
   recorded peering establishes the relationship; never query Azure to resolve
   them.
4. Do not traverse peerings transitively. Include all recorded direct peerings,
   regardless of peering state, because this is an inventory relationship report,
   not a connectivity assessment. State this behavior explicitly in the README.
5. Select every private DNS zone with at least one link whose referenced VNet
   ID is in the eligible set. Do not infer zone applicability from zone names,
   A records, DNS forwarding, or network reachability.
6. Emit one row per distinct matching zone/link pair. A zone linked to several
   eligible VNets therefore has several rows, preserving the matching evidence.
   Deduplicate by normalized zone ID and link ID, not resource name.
7. Use these CSV columns, in this order:
   `privateDnsZoneName,privateDnsZoneId,privateDnsZoneSubscriptionId,privateDnsZoneResourceGroup,virtualNetworkLinkName,virtualNetworkLinkId,linkedVnetName,linkedVnetId,relationshipToHub,hubVnetName,hubVnetId`.
   Set `relationshipToHub` to `hub` or `directPeer`.
8. Use proper CSV quoting and UTF-8 encoding, deterministic ordering, and a
   header-only CSV when there are no matches. Stage output so validation errors
   cannot overwrite a valid report.

Explain that VNet peering does not make private DNS zone links transitive and
does not prove DNS resolution or data-plane connectivity. The CSV lists explicit
zone links to the hub or a direct peer, not zones automatically inherited by peers.

### Validation and documentation

Use mocked Azure CLI responses and local fixtures only. Add focused tests for:

- Both exporters producing schema-equivalent documents for the same mocked
  multi-subscription inventory, including all A-record addresses and child links.
- Empty and single-element collections and paginated responses.
- A failed subscription or child-resource query producing a nonzero exit and no
  partial publication.
- Hub links, outgoing and incoming-only direct peerings, missing remote VNet
  inventory, and exclusion of a peer-of-a-peer.
- Duplicate names with different IDs, case-insensitive ID matching, duplicate
  evidence, multiple matching links per zone, and CSV escaping.
- Empty reports, a missing hub, malformed JSON, and mismatched inventory files.

Document exact supported runtime/dependency versions, how to run tests, both
exporter invocations, and the offline reporting invocation with placeholder IDs.
Explain how to obtain a hub VNet ID, subscription visibility limitations,
non-Connected peering inclusion, and the one-row-per-matching-link CSV granularity.
Include a small representative pair of JSON fixtures and expected CSV as test
data. Run available local validation, report its results honestly, and explicitly
identify anything not tested. Do not execute live Azure commands.
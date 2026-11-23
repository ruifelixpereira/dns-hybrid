# Lab 04 - Align hub and spoke VNet DNS

[Back to the lab index](../README.md)

Configure each tenant's inventoried hub and direct spoke VNets to use that
tenant's Azure DNS Private Resolver inbound endpoint. Keep the resolver VNet
on **Azure-provided DNS**, resetting any custom VNet DNS configuration to default.
All inventoried tenants, including the central tenant, are included by default.

## Exact scope

- The hub is the **reviewed `hubVnetId` in that tenant's Lab 01 inventory**.
  This field originates from a topology heuristic; confirm it names the intended
  hub before applying changes. A missing/uninventoried hub fails validation.
- A spoke qualifies only if **it is itself in the same tenant's inventory** and
  a direct hub peering is recorded at either endpoint. One-sided inventory
  evidence is accepted. `Connected`, `Initiated`, `Disconnected`, and other
  recorded peering states are not filtered.
- The hub is included explicitly, even though it is not its own peer.
- The mapped resolver VNet is excluded from custom DNS alignment and instead
  checked/reset to Azure-provided DNS. Hub and resolver must be distinct.
- VNets absent from the inventory, unpeered VNets, peer-of-a-peer VNets, and
  external/cross-tenant peers are **never updated**, even if discovered live.
- Live peering checks cannot expand this set. If an inventoried spoke's direct
  hub relationship is no longer present live at either endpoint, validation
  stops and requires a refreshed inventory.

Only VNet DNS server lists are replaced. The lab does not change address spaces,
subnets, peerings, routing, DNS records, rulesets, resolver endpoints, NIC DNS
settings, or guest OS DNS configuration.

## Prerequisites

- The full repository checkout. This lab reuses the validated inventory and
  Azure CLI helpers in [Lab 03](../lab-03-cross-tenant-dns-links/README.md), which
  also imports the Lab 01/02 helpers.
- Python 3.10+, Azure CLI, Bash 4.4+ or PowerShell 7.4+. No Python packages needed.
- Azure public cloud and existing healthy Private Resolvers/inbound endpoints.
  Resolver discovery uses the **resolver VNet's resource group**, matching
  Lab 03's current discovery boundary.
- Current Lab 01 combined inventories in `./.inventory`, including the central
  tenant. Confirm their `hubVnetId` values.
- Authorized human Azure CLI access in each selected tenant/subscription, such
  as the central user's accepted guest identities. Cache each destination login
  with `az login --tenant <tenant-id>` before an all-tenant run.
- Read access to the selected subscriptions, affected VNets/peerings, and
  resolver endpoints; `Microsoft.Network/virtualNetworks/write` on each VNet
  requiring changes. Network Contributor at approved scopes is sufficient.
  The lab does not grant roles, create service principals, or sign in automatically.

**DNS reachability is a separate prerequisite.** Hub/spoke peering does not
provide transitive connectivity to a separate resolver VNet. Ensure routing,
firewalls, NSGs, and any required forwarding allow UDP/TCP 53 to the resolver
inbound IP from every selected hub/spoke. Peering presence/state is not proof
that these DNS queries can reach the resolver.

## Provide the per-tenant resolver map

### Generate from the Lab 03 map

Reuse your existing non-central mappings and explicitly add the central resolver:

```bash
bash lab-04-vnet-dns-alignment/scripts/lab.sh generate-map \
  --inventory-directory ./.inventory \
  --central-tenant-id "$TENANT_A_ID" \
  --central-resolver-vnet-id "<full-central-resolver-vnet-arm-id>"
```

```powershell
pwsh -NoProfile -File ./lab-04-vnet-dns-alignment/scripts/Lab.ps1 generate-map `
  --inventory-directory ./.inventory `
  --central-tenant-id "$TENANT_A_ID" `
  --central-resolver-vnet-id "<full-central-resolver-vnet-arm-id>"
```

The command defaults to reading `lab03-resolvers.json` and writing
`lab04-resolvers.json` inside the inventory directory. Override paths with
`--source-resolver-map` and `--output-file`. It preserves the existing entries,
including optional selectors, and derives the central subscription ID from the
supplied VNet ARM ID. Add `--central-inbound-endpoint-id <full-arm-id>` if needed.

Generation is **offline**: no Azure login or resource changes are made. It
validates inventory membership, subscription/tenant mapping, exactly one resolver
per tenant, reviewed hub/resolver separation, and optional inbound selector
ID shape/scope. It cannot prove that an endpoint exists or belongs to the intended
resolver VNet; run `what-if` afterward for live checks.

Existing output is rejected unless `--overwrite` is supplied. The Lab 03 input
cannot be used as the output, even with `--overwrite`; it remains untouched.
Validation failures leave existing output unchanged. Saving uses the same atomic,
owner-only file writer as the backup workflow.

### Manual alternative

Create `./.inventory/lab04-resolvers.json`:

```json
{
  "schemaVersion": 1,
  "resolvers": [
    {
      "tenantId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      "subscriptionId": "11111111-1111-1111-1111-111111111111",
      "resolverVnetId": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network-central/providers/Microsoft.Network/virtualNetworks/resolver"
    },
    {
      "tenantId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      "subscriptionId": "22222222-2222-2222-2222-222222222222",
      "resolverVnetId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/virtualNetworks/resolver",
      "inboundEndpointId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/dnsResolvers/resolver/inboundEndpoints/inbound"
    }
  ]
}
```

Include **exactly one resolver VNet for every inventoried tenant**, including the
central tenant. The VNet must appear in that tenant's inventory and match its
subscription. Hub/spokes may be in other inventoried subscriptions of that same
tenant. `inboundEndpointId` is optional if exactly one healthy endpoint is found;
multiple endpoints require explicit selection. The selected endpoint must belong
to a resolver deployed in the mapped resolver VNet. Its IPv4 addresses are
discovered live, not guessed from an inventory snapshot.

Lab 03's non-central-only map is not sufficient as-is: use a separate Lab 04 map
with the central resolver included. Do not alter the Lab 03 map to include the
central tenant.

## Preview and apply

From the repository root, after signing into the required tenants:

```bash
bash lab-04-vnet-dns-alignment/scripts/lab.sh what-if \
  --inventory-directory ./.inventory \
  --resolver-map ./.inventory/lab04-resolvers.json
```

```powershell
pwsh -NoProfile -File ./lab-04-vnet-dns-alignment/scripts/Lab.ps1 what-if `
  --inventory-directory ./.inventory `
  --resolver-map ./.inventory/lab04-resolvers.json
```

Both launchers run [scripts/align_dns.py](./scripts/align_dns.py) and accept the
same arguments. Add `--tenant-id <id>` to restrict updates to one tenant; the map
must still cover all inventories in the input directory. Alternatively, use a
separate input directory/map containing only intended tenants.

`validate` and `what-if` contact Azure but do not modify VNets or create backups.
Their output is a **script-generated preview, not ARM what-if**. All selected
tenants, topology, endpoints, affected VNets, and write permissions are
preflighted before any update.

Review every old/new DNS server list and the selected hub/resolver IDs. Replace
`what-if` with `create` and type the displayed exact confirmation, for example:

```text
vnet-dns:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa,bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
```

The script:

1. Rechecks inbound IPs before applying the plan.
2. Writes an owner-only `lab04-dns-backup-<unique-id>.json` in the inventory
   directory, capturing each changed VNet's old/new DNS lists and update status.
   Prior backups are not overwritten.
3. Resets the resolver VNet first when necessary, then updates its hub/spokes.
   Matching settings are skipped.
4. Uses only `az network vnet update --dns-servers ...`; clearing custom resolver
   DNS uses `--dns-servers null`, **not** a custom `168.63.129.16` entry.
5. Reads back each VNet and verifies the exact resulting DNS list before
   continuing. Pre-update DNS/ETag checks reject changes observed since preview.

These checks are not an atomic lock across Azure operations. Do not run concurrent
VNet/topology/DNS modifications during this workflow. DNS updates can also occur
before an error or verification failure; no automatic rollback is performed.

## Verify and recover

Inspect VNet DNS settings:

```bash
az network vnet show --ids "<affected-vnet-id>" \
  --query "{id:id,dnsServers:dhcpOptions.dnsServers}" --output json
```

Hub/spoke lists should match their tenant's selected inbound IPs; the resolver
list must be empty/null (Azure-provided DNS). Run the preview again: unchanged
configurations should report no replacements.

Existing clients may need DHCP renewal/restart to adopt new VNet DNS settings.
NIC-level or guest OS DNS overrides can prevent adoption and are not changed.
Test a known private/on-premises record using the client's normal DNS path and,
separately, the resolver inbound IP. ARM configuration success alone is not a
DNS resolution or connectivity test.

To restore a VNet, inspect the printed backup's `dnsBefore` and its status.
`updating` means the outcome requires a live check; it is not a success report.
Restore only the exact reviewed VNet using:

```bash
az network vnet update --ids "<reviewed-vnet-id>" \
  --dns-servers "<previous-ip-1>" "<previous-ip-2>"

# If dnsBefore was empty, restore Azure-provided DNS instead:
az network vnet update --ids "<reviewed-vnet-id>" --dns-servers null
```

Investigate failures and preserve backups before retrying. A later run creates
its own backup with that run's starting settings. Never bulk-reset VNets absent
from the reviewed plan.

## Optional: audit VNet DNS with Azure Policy

The separate [audit-policy.sh](./scripts/audit-policy.sh) and
[Audit-Policy.ps1](./scripts/Audit-Policy.ps1) launchers run
[audit_policy.py](./scripts/audit_policy.py).

As selected for this workflow, it creates a custom policy definition and assignment
in **every enabled subscription covered by each selected tenant's inventory**.
It does not create management-group assignments or discover extra subscriptions.
The definition and assignment are both named `lab04-resolver-dns-audit`.

**The audit scope is broader than the DNS alignment scope.** Every VNet under
each assigned subscription is audited, including unpeered VNets and VNets absent
from the inventory. Only the mapped resolver VNet is excluded from the inbound-DNS
requirement, because it must retain Azure-provided DNS. That exception does not
check/reset the resolver's own configuration; use the alignment workflow for that.

The policy compares the **entire ordered DNS server list** against the same
tenant's live resolver inbound IPs. Azure-provided/empty DNS, missing IPs, extra
DNS servers, duplicates, and a different server order are noncompliant. Other
resource types are not audited. This is configuration auditing, not a DNS
reachability test, and NIC/guest overrides are outside its scope.

The effect is fixed to **Audit**, with assignment enforcement mode
**DoNotEnforce**. This evaluates compliance without enforcing the effect, including
avoiding implicit denial from expression-evaluation failures. It never sets DNS,
blocks deployments, deploys remediation, or creates a managed identity or RBAC
role assignment.

### Preview and create

Use the generated Lab 04 resolver map and existing human tenant logins:

```bash
bash lab-04-vnet-dns-alignment/scripts/audit-policy.sh what-if \
  --inventory-directory ./.inventory \
  --resolver-map ./.inventory/lab04-resolvers.json
```

```powershell
pwsh -NoProfile -File ./lab-04-vnet-dns-alignment/scripts/Audit-Policy.ps1 what-if `
  --inventory-directory ./.inventory `
  --resolver-map ./.inventory/lab04-resolvers.json
```

Add `--tenant-id <tenant-id>` to select one tenant, or omit it for all inventoried
tenants, including central. The map must still cover every inventoried tenant.
The script reads the live inbound IPs, verifies the Azure Policy DNS array alias,
checks subscription ownership/access, and preflights existing definitions,
assignments, and write permissions across **all** selected scopes before mutation.
No hub/peering-based VNet filter is applied to the audit.

Read access to resolver VNets/endpoints, provider aliases, policy definitions, and
assignments is required. Creating policies requires
`Microsoft.Authorization/policyDefinitions/write` and
`Microsoft.Authorization/policyAssignments/write` at each selected subscription.
Network Contributor alone does not supply policy-management permissions; an
appropriately scoped Resource Policy Contributor role is one option.

`validate` and `what-if` are Azure read-only. The preview is script-generated,
not ARM what-if. Replace `what-if` with `create`, review the displayed DNS IPs,
excluded resolver, scopes, and policy IDs, then type the exact prompt:

```text
dns-policy:<sorted-comma-separated-tenant-ids>
```

Existing matching lab-owned definitions/assignments are skipped. A conflicting
definition or a policy name owned by someone else fails without overwriting it.
If resolver IPs change, preview with `--update-existing` and use the same flag
with `create` to refresh this lab's assignments after confirmation. Previous
parameters and enforcement mode are shown for updates. Different definitions,
scope exclusions, selectors, or overrides require manual review, not automatic
replacement. The policy IP parameters are a snapshot; they do not automatically
follow future endpoint IP changes.

Each policy write is read back and verified before continuing. The script checks
for changes since preview but does not hold an atomic lock: avoid concurrent
policy/endpoint changes. Partial policy changes are not automatically rolled back;
the progress output identifies each resource to inspect. No VNet DNS settings
are modified, even after a policy error.

### Review compliance

Assignments do not produce immediate compliance results. Review **Azure Policy >
Compliance** after evaluation, or request a subscription scan:

```bash
az policy state trigger-scan --subscription "$SUBSCRIPTION_1_ID"

az policy state list \
  --subscription "$SUBSCRIPTION_1_ID" \
  --filter "PolicyAssignmentName eq 'lab04-resolver-dns-audit' and ComplianceState eq 'NonCompliant'" \
  --query "[].{resourceId:resourceId,state:complianceState,assignment:policyAssignmentId}" \
  --output table
```

A newly created assignment with no results is not proof of compliance. Other
Azure Policy exemptions or assignments can also affect portal results. Fix DNS
settings separately; the alignment workflow intentionally does not change
unlisted/unpeered VNets even when this policy flags them.

Reference: [Azure Policy rule/value expressions](https://learn.microsoft.com/en-us/azure/governance/policy/concepts/definition-structure-policy-rule),
[ARM array equality](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/template-functions-comparison#equals),
and [assignment enforcement modes](https://learn.microsoft.com/en-us/azure/governance/policy/concepts/assignment-structure#enforcement-mode).

## Local checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s lab-04-vnet-dns-alignment/tests
bash -n lab-04-vnet-dns-alignment/scripts/lab.sh
PYTHONDONTWRITEBYTECODE=1 bash lab-04-vnet-dns-alignment/scripts/lab.sh --help
bash -n lab-04-vnet-dns-alignment/scripts/audit-policy.sh
PYTHONDONTWRITEBYTECODE=1 bash lab-04-vnet-dns-alignment/scripts/audit-policy.sh --help
```

Offline tests verify exact selection fences, resolver exclusion/reset, inbound
selection, DNS-only writes, no-op/read-only behavior, freshness checks, and
partial-failure backup handling. Policy tests also cover audit rule outcomes,
all-subscription/per-tenant parameters, read-only/no-op behavior, conflicts,
explicit assignment refresh, and stopping after write/verification failures.
They do not prove live Azure Policy evaluation or DNS reachability.

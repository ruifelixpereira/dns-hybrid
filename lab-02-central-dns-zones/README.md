# Lab 02 - Central private DNS zones

[Back to the lab index](../README.md)

Create missing private DNS zones in the central tenant from Lab 01's consolidated
`hub-dns-zones.csv`, and ensure **only CSV-listed private DNS zones in the
destination resource group** have a link to the specified resolver VNet.
Remove all links to other VNets from those selected zones. Zones absent from
the CSV are left untouched, even when they share the destination resource group.

| `centralTenantOperation` | Behavior |
| --- | --- |
| `create` | Deploy the zone in the specified central subscription/resource group |
| `repeated` | Do not deploy the zone; manage its links only if it exists in the destination group |
| `none` | Do not deploy the zone; manage its links only if it exists in the destination group |

**No original-tenant resources are deleted.** `originalTenantOperation` is checked
for plan consistency, never executed. DNS records, resolver settings,
and private endpoint DNS zone groups are not migrated or modified. Zones in
other resource groups are not linked or changed.

## Requirements

- Python 3.10 or later (standard library only).
- Azure CLI 2.61 or later with Bicep support; authenticate explicitly before use.
- Bash 4.4 or later, or PowerShell 7.4 or later.
- An **existing** destination resource group in the central subscription.
- The full resource ID of an **existing** resolver VNet in the central tenant
  (its subscription can differ from the destination subscription).
- Contributor-equivalent access to that group, including ARM deployments and
  private DNS zone/link read and write access, link delete access, plus
  subscription/account visibility.
  The resolver VNet also requires read and
  `Microsoft.Network/virtualNetworks/join/action` permissions.

Use the same central tenant chosen during consolidation. The source CSV's
subscription IDs, resource groups, and zone IDs belong to the **original** zones;
they never select the destination. All destination parameters are explicit.
Both launchers call the shared [Python deployment script](./scripts/deploy.py)
so CSV validation, scope checks, and confirmation behave identically.

No Azure deployment is run automatically. Run the following from the repository
root, replacing the example IDs and resource group.

## Bash

```bash
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa

bash lab-02-central-dns-zones/scripts/deploy.sh validate \
  --tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --subscription-id 11111111-1111-1111-1111-111111111111 \
  --resource-group dns-private-zones \
  --resolver-vnet-id /subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver \
  --input-csv ./.inventory/hub-dns-zones.csv
```

Review changes by replacing `validate` with `what-if`. To deploy, replace it with
`create`; the script requires typing the exact displayed
`tenant/subscription/resource-group` confirmation.

## PowerShell

```powershell
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa

pwsh -NoProfile -File ./lab-02-central-dns-zones/scripts/Deploy.ps1 `
  -Action validate `
  -TenantId aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  -SubscriptionId 11111111-1111-1111-1111-111111111111 `
  -ResourceGroup dns-private-zones `
  -ResolverVnetId /subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver `
  -InputCsv ./.inventory/hub-dns-zones.csv
```

Use `-Action what-if` to review or `-Action create` to deploy with confirmation.
The launcher discovers `python3` or `python` on PATH.

Both scripts default to `./inventory/hub-dns-zones.csv` if the CSV path is omitted.
They verify the destination and resolver subscriptions' tenants, read the existing
resource group and VNet, then list destination zones and read links only for
zones whose normalized names appear in the CSV before deployment.
They never sign in automatically, select a default
subscription, or create a resource group.

## Plan validation and deployment

The CSV must have the seven Lab 01 columns, with unique headers and nonempty
metadata. Unknown operations, malformed rows, invalid GUIDs, conflicting
`create`/`none` domains, and operations inconsistent with the specified central
tenant stop execution before Azure calls. Zone names are normalized to lowercase
without trailing dots and deduplicated by domain. Azure validates the remaining
service-specific naming restrictions.

Even if no zones are marked `create`, the scripts check CSV-listed zones that
exist in the destination group, deploy missing resolver links, and remove links
to other VNets only from those zones. Rows marked `none` or `repeated` never
create a missing zone. An empty CSV (headers only) changes no zones or links. If there
are also no missing links or links to remove, they succeed with an explicit
diagnostic without a deployment or confirmation.
If the CSV has no central-tenant rows, it cannot identify
the intended central tenant independently; verify the explicit destination.

The [Bicep template](./main.bicep) is resource-group scoped, uses global private
DNS zones, and is deployed in **Incremental** mode. Plans above the ARM/Bicep
800-resource limit are processed in batches of at most 400 planned zones with
their missing links, followed by batches of at most 800 other missing links.
New zones and their links are deployed together, with explicit dependencies.
There is one confirmation for the entire plan, including link removals.
Only `create` deletes links, using explicit Azure CLI link-delete calls after
all deployment batches succeed. Cleanup-only plans also require confirmation.
Failures stop later batches or deletions; successful earlier changes remain
applied. A deployment failure prevents all deletions. Do not run simultaneous
deployments or link changes in the same group.

Links are matched by the target VNet resource ID, case-insensitively, not by link
name. Existing resolver links are left unchanged (including their registration
settings); a link that is not successfully provisioned stops execution.
New links use a deterministic name and **auto-registration disabled**.
A name collision with a different VNet uses an available numeric suffix instead
of overwriting that link. `validate` and `what-if` include missing links and
list each other-VNet link scheduled for removal, but never create or delete
links. Link deletions are not part of the Bicep what-if output or ARM validation.
After the ARM preview, `what-if` prints a separate **Planned VNet link deletions**
section to standard output, with the deletion count, each link's full resource ID,
and its target VNet ID. This includes CSV-listed existing zones and cleanup-only
plans; a zero count explicitly indicates that no selected links need removal.
Removing other VNet links can break DNS resolution for those VNets; review the
listed removals and ensure they use the central resolver before running `create`.

An existing same-name zone in the destination group is an ARM resource update,
not a second zone; child records are not declared or deleted. Other-VNet links
are removed explicitly, not through the Incremental deployment. Inventory
can become stale, and a same-domain zone elsewhere in the tenant is not detected
by this deployment. Regenerate and review the consolidated plan before use.
Creating empty zones and resolver links does **not** migrate DNS records or
make it safe to delete original zones. Verify records, forwarding, and actual
DNS resolution separately.

## Offline tests and Bicep validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab-02-central-dns-zones/tests -p 'test_deploy.py' -v
bash -n lab-02-central-dns-zones/scripts/deploy.sh
az bicep build --file lab-02-central-dns-zones/main.bicep --stdout
```

Tests use a strict local Azure CLI mock, never a live tenant. PowerShell cases
are skipped when `pwsh` is unavailable; set `PWSH` to its executable to include
them. Actual Azure permissions, service validation, costs, and deployment must
be checked separately by the operator.

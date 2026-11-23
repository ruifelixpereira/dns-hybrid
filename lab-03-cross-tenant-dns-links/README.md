# Lab 03 - Cross-tenant private DNS links

[Back to the lab index](../README.md)

Link selected private DNS zones in the **central tenant** to existing Azure DNS
Private Resolver VNets in **non-central tenants**, using a central
user-assigned managed identity and Azure Lighthouse for automation.

Interactive tests use the central human user, already invited as a guest into
the other tenants with the required Azure permissions. The lab provisions only
the managed identity; it does not deploy an execution host for that identity or
create an application, a separate service principal, or client credentials.

An optional, separate forwarding workflow also configures on-premises domain
rules on each non-central resolver's outbound endpoint and links its ruleset to
the resolver VNet. It can first export a selected central ruleset into an inventory
file, then reuse those rules across all mapped non-central tenants. It is not run
automatically by the private-zone link workflow.

> **Implementation status:** Focused offline regression tests cover the identity
> refactor. The complete workflow has
> not been verified against live Azure tenants. Review the scripts, permissions,
> and previews before applying changes. This runbook is not a claim that the
> cross-tenant authorization path has been tested in your environment.

## Contents

1. [Architecture and scope](#architecture-and-scope)
2. [Prerequisites and permissions](#prerequisites-and-permissions)
3. [Files, commands, and actions](#files-commands-and-actions)
4. [Prepare inventories and resolver mappings](#1-prepare-inventories-and-resolver-mappings)
5. [Invite the central operator into non-central tenants](#2-invite-the-central-operator-into-non-central-tenants)
6. [Create the central managed identity and DNS permissions](#3-create-the-central-managed-identity-and-dns-permissions)
7. [Delegate non-central resolver resource groups](#4-delegate-non-central-resolver-resource-groups)
8. [Sign in for interactive testing or managed-identity execution](#5-sign-in-for-interactive-testing-or-managed-identity-execution)
9. [Preview and create the DNS links](#6-preview-and-create-the-dns-links)
10. [Verify permissions, links, and DNS resolution](#7-verify-the-result)
11. [Configure on-premises DNS forwarding](#8-configure-on-premises-dns-forwarding)
12. [Reruns and operational limitations](#reruns-and-operational-limitations)
13. [Troubleshooting](#troubleshooting)
14. [Rollback](#rollback)
15. [Local checks and references](#local-checks)

## Architecture and scope

```text
Central tenant
  Human operator
    - owns the initial setup process
    - is invited as a guest into each non-central tenant
    - still requires separate directory and Azure permissions

  Central DNS resource group
    CSV-listed private DNS zones
      + existing central resolver link, preserved
      + link to selected non-central resolver VNet A
      + link to selected non-central resolver VNet B

  Human operator                    User-assigned managed identity
    Tests from laptop                 Azure-hosted automation
    Existing DNS/network RBAC         Private DNS Zone Contributor
    in the relevant tenants           on central DNS resource group
                                           |
                 Azure Lighthouse
                  /              \
Non-central tenant A              Non-central tenant B
  Resolver resource group           Resolver resource group
    Network Contributor               Network Contributor
    for central managed identity      for central managed identity
```

In Lighthouse terminology, the central tenant is the **managing tenant**.
Each non-central tenant owns resources being delegated to it.
Delegations are deployed **in the non-central tenants**, not in the central DNS
resource group.

### What the lab changes

- Assumes the central operator is already an accepted guest in each selected
  non-central tenant. Manual invitation instructions remain below, with optional
  invitation helpers for environments that still need bootstrap.
- Creates or reuses a central user-assigned managed identity only.
- Grants that identity **Private DNS Zone Contributor** on the explicitly
  selected central DNS resource group.
- Creates Lighthouse authorizations granting only that managed identity
  **Network Contributor on each selected resolver VNet's entire resource group**.
- Adds missing links from selected central zones to explicitly mapped remote
  resolver VNets, with auto-registration disabled.
- Before adding each missing central link, removes same-name private DNS zone
  links in non-central tenants only when they target that mapped resolver VNet.
  These removals are live-preflighted, previewed, and explicitly confirmed.

### What the lab must not change

- **Zones whose normalized names are absent from `hub-dns-zones.csv`**, even
  when they are in the same central resource group.
- Existing central VNet links, unrelated non-central VNet links, and their
  registration settings. The only link-removal exception is the same-name
  non-central resolver link described above.
- DNS records are not explicitly migrated or edited. **Azure deletes
  auto-registered records when an auto-registration-enabled link is removed**;
  review this consequence before confirming a replacement.
- Original private DNS zones in non-central tenants.
- VNet peerings, resolver endpoints, custom DNS settings,
  private endpoints, or private endpoint DNS zone groups.
- Existing forwarding rules and ruleset endpoint associations. The optional
  forwarding workflow adds missing requested rules and VNet links but does not
  overwrite conflicting rules, remove existing rules, or rebind endpoints.

The link target set is:

```text
CSV-listed zones in the specified central DNS resource group
    x
explicitly mapped non-central resolver VNets
```

Zone names are deduplicated and normalized using Lab 02's CSV validation.
Rows marked `create`, `none`, or `repeated` can select an existing central zone;
Lab 03 does not create missing zones. A missing selected zone fails the live
preflight. A header-only CSV performs no link lookups or link changes, although
inventory, mapping, and identity-state validation still occurs.

### Important permission boundary

Lighthouse supports subscription and resource-group delegation, not a
VNet-only delegation. Here, Network Contributor covers **all networking
resources in the resolver VNet's current resource group**, not just the VNet.
An additional VNet-level role assignment would not narrow that broader grant.

Review every resource-group scope displayed before confirming onboarding.
If that boundary is unacceptable, stop and redesign the resource grouping or
access model; do not apply this lab unchanged.

### Important interaction with Lab 02

**Run Lab 02 before Lab 03.** Lab 02's `create` workflow removes non-resolver
links from its selected zones, retaining only its configured resolver VNet.
Rerunning that cleanup against the same zones after this lab can remove the
cross-tenant links added here.

Lab 03 preserves existing central links and does not change Lab 02's behavior.
Lab 01 exports A record sets but Lab 02 does not import them into central zones.
Verify record migration separately before replacing any non-central resolver link.

## Prerequisites and permissions

### Tools and existing resources

- The complete repository checkout, including Labs 01 and 02. The shared
  Python implementation imports their inventory and CSV validation helpers.
- Python 3.10 or later; standard library only.
- Azure CLI with the commands and API versions used by the scripts.
  Verify command availability with `az version` and the help commands below.
- Bash 4.4 or later, or PowerShell 7.4 or later.
- Bicep support for Lighthouse deployment.
- Azure public cloud. The current implementation uses
  `management.azure.com` and `graph.microsoft.com` explicitly; sovereign clouds
  are not supported by these scripts.
- An existing central DNS resource group with the selected zones, prepared
  through [Lab 02](../lab-02-central-dns-zones/README.md).
- An existing central resource group in which to create the managed identity.
  It may be the DNS resource group, but the examples use a separate group.
- Existing non-central resolver VNets and successfully provisioned DNS Private
  Resolvers. The current preflight enumerates resolvers **in each VNet's resource
  group**; a resolver resource in another group is not discovered by it.
- Lab 01 combined inventories and the consolidated CSV. See
  [Lab 01](../lab-01-inventory/README.md).

Scripts do not create resource groups, automatically sign in, or set a new
default subscription. Operator-run `az login` commands below deliberately change
the local Azure CLI authentication context.

### Identity and permission matrix

| Step | Identity executing it | Required access |
| --- | --- | --- |
| Invite central user | Authorized human in each non-central tenant | Graph guest lookup access, invitation permission such as `User.Invite.All`, applicable consent, and tenant policy allowing invitations |
| Accept invitation | Existing central user | Access to the invitation and compliance with destination MFA, Conditional Access, and external collaboration policies |
| Create managed identity | Central human operator | Managed Identity Contributor or equivalent permissions in the identity resource group |
| Assign central DNS role | Central human operator | Role-assignment write rights at the DNS group, with the read access used by the script |
| Deploy Lighthouse | Central user signed in as a guest in each non-central tenant, or an authorized local human | Subscription onboarding permissions, deployment permissions, and resource/read access described below |
| Test central DNS links interactively | Central human user, also a guest in each non-central tenant | Existing Private DNS Zone Contributor on the central DNS group and Network Contributor (or equivalent required permissions) on the remote resolver resources; guest membership alone is insufficient |
| Create central DNS links with automation | Central managed identity attached to a supported Azure host | Private DNS Zone Contributor on the central DNS group and Lighthouse Network Contributor over each selected remote resolver group, plus host attachment |
| Replace conflicting non-central DNS links | The identity executing `links` | Visibility/read access to private zones in the inventory-covered non-central subscriptions and read/delete permissions on each exact conflicting link; Private DNS Zone Contributor at its zone scope is sufficient for link deletion |

**Directory roles and Azure roles are different.** Being a guest does not grant
Owner, Network Contributor, or directory administration. A subscription Owner
does not automatically have the Graph permissions needed to invite guests.

For Lighthouse onboarding, Microsoft documents an account in the resource-owning
tenant with subscription permissions including:

- `Microsoft.Authorization/roleAssignments/read`
- `Microsoft.Authorization/roleAssignments/write`
- `Microsoft.Authorization/roleAssignments/delete`

Subscription Owner is a sufficient broad role, but is not something this lab
automatically assigns. Have an administrator approve the appropriate access.
The deployment also needs the applicable deployment and ManagedServices
permissions. The script checks subscription-level onboarding rights, so
resource-group Network Contributor alone is insufficient.

The scripts also perform live subscription, resource-group, VNet, resolver, and
effective-permission queries. An inability to perform those reads stops the
workflow; it is not treated as an empty result. If your permission design
deliberately excludes subscription visibility, review these preflight
requirements before proceeding rather than adding broad roles automatically.

The managed identity is represented by its **principal/object ID** in Lighthouse
authorizations. Its client ID is not interchangeable with that object ID.
Azure represents managed identities as service-principal objects internally;
using `ServicePrincipal` as the Azure RBAC principal type does not create a
separate application or password-based service principal.

The scripts do not assign roles to the human test user or include that user in
Lighthouse authorizations. An administrator must grant the user's approved
permissions separately in each tenant.
The UAMI's Lighthouse grant covers only resolver resource groups. If local zones
are elsewhere, it may not authorize their discovery or link removal. The lab
does not widen Lighthouse scopes or automatically grant cleanup permissions.

## Files, commands, and actions

| File | Purpose |
| --- | --- |
| [scripts/lab.py](./scripts/lab.py) | Shared Python orchestration and validation |
| [scripts/lab.sh](./scripts/lab.sh) | Bash entry point |
| [scripts/Lab.ps1](./scripts/Lab.ps1) | PowerShell entry point |
| [scripts/setup-guests.sh](./scripts/setup-guests.sh) | Separate Bash guest invitation entry point |
| [scripts/Setup-Guests.ps1](./scripts/Setup-Guests.ps1) | Separate PowerShell guest invitation entry point |
| [scripts/forwarding.py](./scripts/forwarding.py) | Separate on-premises forwarding discovery, reconciliation, and creation |
| [scripts/setup-forwarding.sh](./scripts/setup-forwarding.sh) | Bash forwarding entry point |
| [scripts/Setup-Forwarding.ps1](./scripts/Setup-Forwarding.ps1) | PowerShell forwarding entry point |
| [lighthouse.bicep](./lighthouse.bicep) | Subscription-scoped Lighthouse definition and resource-group-scoped assignment |
| [lighthouse-assignment.bicep](./lighthouse-assignment.bicep) | Resource-group module containing only the Lighthouse assignment |

Run examples from the **repository root**. Replace all example IDs, names, email
addresses, locations, and paths before use.

```bash
bash lab-03-cross-tenant-dns-links/scripts/lab.sh --help
bash lab-03-cross-tenant-dns-links/scripts/lab.sh identities --help
bash lab-03-cross-tenant-dns-links/scripts/lab.sh delegate --help
bash lab-03-cross-tenant-dns-links/scripts/lab.sh links --help
bash lab-03-cross-tenant-dns-links/scripts/setup-guests.sh --help
bash lab-03-cross-tenant-dns-links/scripts/setup-forwarding.sh --help
```

The PowerShell launchers accept the **same Python-style argument names**:
use `--central-tenant-id`, not `-CentralTenantId`.

| Workflow | `validate` | `what-if` | `create` |
| --- | --- | --- | --- |
| `discover` | Not applicable: discovery itself is offline | Not applicable | Not applicable |
| `guests` | Read current account/guests; show invitation plan | Same script-generated preview | Invite a missing guest after confirmation |
| `identities` | Read current resources/permissions; show identity plan | Same script-generated preview | Create the missing managed identity and assign its DNS role after confirmation |
| `delegate` | Preflight and run ARM deployment validation for missing delegations | Preflight and run ARM deployment what-if for missing delegations | Deploy missing delegations after confirmation |
| `links` | Preflight and show central additions and conflicting local-link removals | Same script-generated preview | Remove confirmed same-name non-central resolver links, then add their missing central links |
| Separate forwarding script | Validate inputs and live resources; show forwarding plan | Same script-generated preview, not ARM what-if | Create missing rulesets, requested rules, and VNet links after confirmation |

Only delegation deployment uses native ARM what-if. Invitations, identity/RBAC
operations, forwarding setup, and direct CLI link creation have **script-generated** previews.
Read-only actions still contact Azure except for `discover`.
The forwarding script's `export` action reads central Azure resources and writes
a local inventory rules file; it does not create or modify Azure resources.

The current CLI wrapper captures Azure JSON responses. Delegation what-if uses
`--no-pretty-print` with `--output json`; without that switch, Azure CLI's formatted
what-if output is not JSON even when JSON output is requested. Delegation
what-if responses are not printed as a full native resource diff; the script
prints the proposed resource-group grants and fails on CLI errors. Do not
interpret a short delegation preview as evidence of a reviewed native ARM diff.

## 1. Prepare inventories and resolver mappings

### Establish explicit parameters

Example values used throughout this runbook:

| Parameter | Example |
| --- | --- |
| Central tenant | `aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` |
| Central subscription | `11111111-1111-1111-1111-111111111111` |
| Central DNS group | `dns-private-zones` |
| Central identity group | `central-identities` |
| Managed identity name | `dns-cross-tenant-lab03` |
| Managed identity location | `westeurope` |
| Non-central tenant | `bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb` |
| Non-central subscription | `22222222-2222-2222-2222-222222222222` |
| Non-central resolver group | `network-noncentral` |
| Non-central resolver VNet name | `resolver` |

Protect local inventory/state files. They contain infrastructure identifiers and
may contain private network information. Do not commit real copies.

Expected input layout:

```text
.inventory/
  <central-tenant-id>-inventory.json          # optional central inventory
  <non-central-tenant-id>-inventory.json      # one per non-central tenant
  hub-dns-zones.csv
  lab03-resolvers.json                       # operator-created mapping
```

### Discover non-central tenants offline

```bash
bash lab-03-cross-tenant-dns-links/scripts/lab.sh discover \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --inventory-directory ./.inventory
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Lab.ps1 discover `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --inventory-directory ./.inventory
```

Discovery validates Lab 01 combined inventory documents, excludes the explicit
central tenant, and reports the remaining tenant IDs and covered subscriptions.
Duplicate tenants, conflicting subscription ownership, malformed inventories,
and duplicate zone/VNet IDs fail validation.

`suggestedHubVnetId` comes from Lab 01's topology heuristic. It is **not**
automatically selected and is not proof that the VNet hosts a DNS resolver.

### Create the explicit resolver mapping

Create `./.inventory/lab03-resolvers.json` with this structure:

```json
{
  "schemaVersion": 1,
  "resolvers": [
    {
      "tenantId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      "subscriptionId": "22222222-2222-2222-2222-222222222222",
      "resolverVnetId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/virtualNetworks/resolver"
    }
  ]
}
```

Include at least one entry for **every** discovered non-central tenant.
Multiple VNets per tenant and multiple subscriptions are supported by the
mapping. Do not include the central tenant or the same VNet twice.

Each VNet must appear in that tenant's inventory and agree with the specified
subscription. Live preflight subsequently verifies the resource and resolver.
The mapping is not permission to use a guessed or stale VNet ID.

If the directory contains tenants that do not belong in this run, prepare a
separate input directory containing the intended inventories; do not silently
drop tenants from a mapping of the original directory.

## 2. Invite the central operator into non-central tenants

For normal testing, assume this is already complete and verify the accepted
guest and its Azure RBAC access. Keep the following instructions for manual
bootstrap when needed. A central user who is not yet a guest cannot generally
invite itself into another organization.

Repeat this section for each non-central tenant reported by discovery.

### Sign in as an authorized inviter in the destination tenant

```bash
az login --tenant bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb --allow-no-subscriptions
az account show --query "{tenantId:tenantId,user:user}" --output json
```

The account must be an authorized **human** in this destination tenant.
`--allow-no-subscriptions` supports directory-only invitation setup; it does not
grant subscription permissions.

Graph invitation creation requires the appropriate consent and invitation
permissions. Guest lookup also requires permission to read the relevant user
properties. Tenant external collaboration policy can impose additional
restrictions. An administrator may need to perform this bootstrap.

### Optional helper: preview, then send the invitation

```bash
bash lab-03-cross-tenant-dns-links/scripts/setup-guests.sh what-if \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --tenant-id bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
  --inventory-directory ./.inventory \
  --user-email operator@central.example
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Setup-Guests.ps1 what-if `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --tenant-id bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb `
  --inventory-directory ./.inventory `
  --user-email operator@central.example
```

Replace `what-if` with `create` to send the invitation. Type the exact confirmation:

```text
guest:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
```

The script uses Microsoft Graph to look up guests by invitation email and
`otherMails`. An existing unique match is reported without resetting redemption
or sending another invitation. Multiple matches are an error.

Directory queries use eventual consistency. If you have just invited a user,
wait for directory replication and verify the existing guest in Entra before
rerunning; do not repeatedly submit invitations.

### Manual alternative and invitation acceptance

1. In the **non-central tenant**, open Microsoft Entra ID in the Azure portal.
2. Under Users, choose **New user > Invite external user**.
3. Invite the existing central user's email address.
4. The central user opens the invitation and accepts using its central identity.
5. Verify the destination directory contains the intended guest and its
   invitation has been accepted.
6. A destination administrator grants the guest the approved Azure onboarding
   permissions described in the permission matrix.

Record the **guest object ID from that destination directory** if assigning
Azure roles manually. It differs from the central user's home-directory object
ID. Do not derive a guest UPN or guess an object ID from the email address.

Acceptance and administrator authorization are separate from invitation creation.
The invitation script does not automatically grant Azure roles.

## 3. Create the central managed identity and DNS permissions

Sign in as the existing central user, now using its **central** context:

```bash
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
az account show --subscription 11111111-1111-1111-1111-111111111111 \
  --query "{tenantId:tenantId,user:user}" --output json
```

Verify both resource groups already exist. Select the intended managed identity
name; ambiguous managed-identity matches are rejected.

### Preview the managed identity and role assignment

```bash
bash lab-03-cross-tenant-dns-links/scripts/lab.sh identities what-if \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --central-subscription-id 11111111-1111-1111-1111-111111111111 \
  --dns-resource-group dns-private-zones \
  --identity-resource-group central-identities \
  --location westeurope \
  --name dns-cross-tenant-lab03 \
  --state-file ./.inventory/lab03-identities.json
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Lab.ps1 identities what-if `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --central-subscription-id 11111111-1111-1111-1111-111111111111 `
  --dns-resource-group dns-private-zones `
  --identity-resource-group central-identities `
  --location westeurope `
  --name dns-cross-tenant-lab03 `
  --state-file ./.inventory/lab03-identities.json
```

### Apply

Replace `what-if` with `create`. No client credentials are generated.

Type the displayed confirmation:

```text
identities:/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/dns-private-zones
```

The workflow:

1. Creates or reuses the user-assigned managed identity.
2. Writes secret-free identity metadata to the state file.
3. Ensures the managed identity has **Private DNS Zone Contributor** on the DNS group.

The DNS role ID is `b12aa53e-6015-4669-85d0-8515ebb3ae7f`.
The managed identity's principal type in Azure RBAC is `ServicePrincipal`.

### State handling and existing deployments

The identity state contains:

- `schemaVersion: 2`, central tenant/subscription and DNS resource-group name.
- Managed identity `clientId`, `objectId`, and ARM `resourceId`.
- IDs of resources/assignments recorded as created by the identity workflow.

State files retain owner-only POSIX permissions or current-user Windows ACLs.

Version 1 state from the former dual-identity workflow is rejected before identity
setup changes anything. Preserve that file as a cleanup record and use a **new
state-file path** with `identities`. The existing UAMI can be reused by its name
and resource group; the new state records only changes made by the new workflow.

Existing dual-identity Lighthouse definitions are also rejected, not silently
rewritten. An authorized administrator must review dependent workloads, explicitly
remove the legacy registration assignment and its unreferenced definition, then
rerun `delegate` with the new state. This temporarily interrupts delegated access.
Separately review and revoke old application credentials, remove obsolete role
assignments, and delete an old application only if no other workload needs it.
The refactor does not automatically remove any legacy Azure resources.

## 4. Delegate non-central resolver resource groups

Repeat this section for **each non-central tenant**. Use the same identity-state
file and complete resolver mapping throughout.

### Sign in as the central human in the destination tenant

After guest acceptance and administrator RBAC assignment:

```bash
az login --tenant bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
az account show --subscription 22222222-2222-2222-2222-222222222222 \
  --query "{tenantId:tenantId,user:user}" --output json
```

Alternatively, an authorized local human administrator can deploy the delegation.
The managed identity cannot bootstrap its own Lighthouse access.

An authorized administrator must ensure the resource provider is registered:

```bash
az provider show --subscription 22222222-2222-2222-2222-222222222222 \
  --namespace Microsoft.ManagedServices --query registrationState --output tsv
```

If registration is approved and required:

```bash
az provider register --subscription 22222222-2222-2222-2222-222222222222 \
  --namespace Microsoft.ManagedServices --wait
```

The lab script checks registration but does not issue this registration command
automatically.

### Validate and preview the delegation

```bash
bash lab-03-cross-tenant-dns-links/scripts/lab.sh delegate what-if \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --tenant-id bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
  --inventory-directory ./.inventory \
  --resolver-map ./.inventory/lab03-resolvers.json \
  --state-file ./.inventory/lab03-identities.json
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Lab.ps1 delegate what-if `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --tenant-id bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb `
  --inventory-directory ./.inventory `
  --resolver-map ./.inventory/lab03-resolvers.json `
  --state-file ./.inventory/lab03-identities.json
```

Use `validate` for ARM validation or `create` to deploy. Confirm:

```text
delegate:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
```

For each selected resolver resource group, the template creates a registration
definition **at subscription scope** and a registration assignment **only at that
resource group's scope**, referencing the subscription definition. The definition
alone grants no access; no subscription-wide assignment is created.

The script uses `az deployment sub` for validation, what-if, and creation. Its
deployment metadata location comes from the existing resolver resource group's
location; this does not move or create any resource group.

Each definition contains:

- `managedByTenantId`: the central tenant ID.
- One authorization for the **central managed identity principal/object ID**.
- Network Contributor role ID:
  `4d97b98b-1d4f-4787-a291-c67834d212e7`.

Do not substitute application/client IDs, guest user IDs, or principal IDs from
another directory. The authorization refers only to the central managed identity.

Existing matching healthy delegations are preserved. Conflicting definitions,
assignments, principals, roles, or unhealthy provisioning states stop the
workflow instead of being overwritten. Multiple resolver VNets in the same
resource group normally share the same delegation scope.

Check the delegation in the non-central tenant's **Service providers** view.
Allow time for authorization propagation before testing automation access.

## 5. Sign in for interactive testing or managed-identity execution

### Check existing Lighthouse delegations

As an authorized human, sign in to the **non-central tenant that owns the
delegated resources**. Repeat these read-only checks for each mapped tenant and
resolver resource group:

```bash
az login --tenant bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb

# Lab 03's resource-group-scoped assignments, including their definitions.
az rest --method get \
  --subscription 22222222-2222-2222-2222-222222222222 \
  --url "https://management.azure.com/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.ManagedServices/registrationAssignments?api-version=2022-10-01&%24expandRegistrationDefinition=true" \
  --output json

# Subscription-level assignments; check resource-group assignments separately.
az managedservices assignment list \
  --subscription 22222222-2222-2222-2222-222222222222 \
  --include-definition true \
  --output json

# Definitions are always listed at subscription scope.
az managedservices definition list \
  --subscription 22222222-2222-2222-2222-222222222222 \
  --output json
```

The resource-group check uses `az rest` to avoid an Azure CLI
`az managedservices assignment list --resource-group` bug that can raise
`TypeError: can only concatenate str (not "AAZSimpleValue") to str`.
`%24expandRegistrationDefinition=true` includes definition details without shell
expansion of the API's `$` query parameter. If the REST response contains a
`nextLink`, retrieve that URL with another `az rest --method get` using the same
subscription to inspect the remaining assignments.

Verify that the resolver group's assignment references the intended
subscription-scoped definition, its managing tenant is the central tenant, and
its authorization grants Network Contributor to the UAMI object ID recorded in
the identity-state file. A definition alone does **not** activate delegated
access; a registration assignment must reference it. Confirm no unintended
resource group or subscription-wide assignment was added.

In the Azure portal, inspect **Azure Lighthouse > Service providers** in the
resource-owning tenant. **My customers** in the central managing tenant requires
an identity authorized through Lighthouse; the human user's guest permissions
alone do not grant that view.

These CLI commands also work in PowerShell. They do not create or modify
delegations. Return to the central sign-in context below before testing links.

### Interactive laptop tests: central human user

Use the existing central user, already an accepted guest in each non-central
tenant. Its central identity needs DNS permissions, and each destination guest
needs the approved network/read permissions. The UAMI's Lighthouse grant does
**not** grant the human user access.

Sign in to each destination tenant first so Azure CLI can access its subscription,
then sign in to the central tenant for the link workflow:

```bash
# Repeat for each non-central tenant.
az login --tenant bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
az account show --subscription 22222222-2222-2222-2222-222222222222 \
  --query "{tenantId:tenantId,user:user}" --output json

az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
```

These Azure CLI commands also work in PowerShell. Confirm the central account:

```bash
az account show --subscription 11111111-1111-1111-1111-111111111111 \
  --query "{tenantId:tenantId,user:user}" --output json
```

The expected user type for interactive testing is `user`. The link workflow
checks the account and token tenant and requires a valid token object ID.
Remote preflight requests select the mapped subscription explicitly so that
Azure CLI can use the appropriate guest context.

### Azure-hosted automation: configured managed identity

The identity has no downloadable client secret. On a supported Azure host
**after** assigning that managed identity to the host:

```bash
az login --identity --client-id "<managed-identity-client-id>"
```

Then run the same `links` commands below. The workflow checks the token's tenant
and object ID against the configured UAMI. Other application/service-principal
identities are rejected. Azure CLI may represent a managed-identity account as
`servicePrincipal`; the token object ID, not its account display name, identifies
the configured UAMI.

Do not use `az login --identity` on a laptop. A successful human-user test does
not validate the UAMI's Lighthouse authorization; test that path separately from
the Azure host. Neither path has been verified end-to-end against live tenants
by the offline tests.

## 6. Preview and create the DNS links

Remain signed in as the **central human user** for interactive testing, or as the
configured UAMI on its Azure host.

### Read-only preview

```bash
bash lab-03-cross-tenant-dns-links/scripts/lab.sh links what-if \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --inventory-directory ./.inventory \
  --resolver-map ./.inventory/lab03-resolvers.json \
  --state-file ./.inventory/lab03-identities.json \
  --input-csv ./.inventory/hub-dns-zones.csv
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Lab.ps1 links what-if `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --inventory-directory ./.inventory `
  --resolver-map ./.inventory/lab03-resolvers.json `
  --state-file ./.inventory/lab03-identities.json `
  --input-csv ./.inventory/hub-dns-zones.csv
```

The central subscription and DNS group come from the validated identity state.
The mapping supplies the remote tenant/subscription/VNet IDs.

The workflow preflights:

1. The signed-in central human user or configured managed identity and token identity.
2. The central subscription, DNS group, and required DNS permissions.
3. Mapped subscriptions, VNets, and healthy DNS Private Resolvers.
4. Effective remote VNet read and `join/action` permissions.
5. Every CSV-selected central zone and its existing links.
6. Live private zones in the inventory-covered subscriptions of the inventoried
   non-central tenants, filtering to the selected zone names before reading links.
7. Same-name non-central links targeting a mapped resolver VNet, their healthy
   provisioning state, ETags, and exact link read/delete permissions.

It then prints a script-generated plan with the exact central links to create
and non-central link IDs to delete. `validate` and `what-if` never delete links.
Subscriptions missing from the inventories or zones hidden from the caller are
outside the discovery coverage; refresh inventories and verify access first.

If the resolver VNet is **already linked to the selected central zone**, that
healthy link is kept regardless of its name or registration setting, and no
non-central replacement is performed for that pair.

Otherwise, the script removes only links whose zone has the **same normalized
name**, whose zone belongs to an inventoried non-central tenant, and whose target
is that **exact mapped resolver VNet**. It preserves the non-central zone itself,
manual DNS records, links to other VNets, and unselected namespaces. It does not
remove links from a different central zone or parent/child namespace to bypass
an overlapping-namespace error.

New central link names are derived deterministically from the target
VNet ID. A collision with an existing different-VNet link stops execution; it
does not overwrite or delete the existing link.

### A-record exports are not record migration

Lab 01 exports **all A record sets visible to its exporting account** in the
covered enabled subscriptions, including apex `@` and auto-registered records.
It follows pagination and stores them as `privateDnsZones[].aRecordSets` in each
tenant inventory, with record names, FQDNs, TTLs, and IPv4 addresses. This is a
point-in-time snapshot, not proof that all tenants/subscriptions were visible.
It does not export all other record types, such as CNAME, AAAA, TXT, or SRV.

**Neither Lab 02 nor Lab 03 copies those exported records into the central
zones.** Before replacing a local link, migrate and verify every record required
by the affected workloads in the central zone. A link to an empty central zone
will not provide those answers.

Removing an auto-registration-enabled link also causes Azure to delete its
auto-registered records in the local zone. The preview displays
`registrationEnabled` for each removal. New central links have auto-registration
disabled, so they will not recreate those records automatically.

### Apply

Replace `what-if` with `create` and review the plan again. Confirm:

```text
links:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/11111111-1111-1111-1111-111111111111/dns-private-zones
```

For each missing pair, the script first deletes its previewed conflicting
non-central links using their recorded ETags (`--if-match`) and verifies each
link is absent. A concurrent change causes the conditional deletion to fail
rather than deleting a modified link.

It then executes the equivalent of:

```bash
az network private-dns link vnet create \
  --subscription 11111111-1111-1111-1111-111111111111 \
  --resource-group dns-private-zones \
  --zone-name "<CSV-selected-existing-zone>" \
  --name "<deterministic-link-name>" \
  --virtual-network "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/virtualNetworks/resolver" \
  --registration-enabled false
```

**The command targets the central subscription**, because that is where the
private DNS zone and link resources live. The VNet parameter is a full ARM ID
in a non-central subscription. Lighthouse supplies delegated access to that
remote resource for managed-identity execution without signing in to each tenant.
Interactive human tests instead use the user's existing guest access; no user
Lighthouse authorization is created.

The script checks the create response's target VNet and successful provisioning
state. It stops on the first failure and reports each earlier verified removal
and creation. **There is no automatic rollback.** If local unlinking succeeds but
central creation fails, the resolver can lose access to that namespace until
the central link is created or the local link is deliberately restored. A
record backup is not restored by recreating the link; verify auto-registered
records separately.

## 7. Verify the result

### Confirm the central DNS roles

As an authorized central human, inspect IAM on the central DNS resource group.
The managed identity's principal object ID should have Private DNS Zone Contributor.
The managed identity need not have permission to enumerate all role
assignments, so use an administrator when checking IAM.
Check the human test user's central and destination roles separately.

### Confirm non-central delegations

As an authorized human in each non-central tenant:

- Inspect the selected resolver resource group's Lighthouse registration.
- Confirm the managing tenant is the central tenant.
- Confirm only the central managed identity principal object ID has Network Contributor.
- Confirm no unintended resource group was delegated.

Lighthouse access is expressed through registration resources; do not assume it
will appear exactly like an ordinary local IAM role assignment.

### Inspect links on an individual selected zone

As the central human user or configured managed identity:

```bash
az network private-dns link vnet list \
  --subscription 11111111-1111-1111-1111-111111111111 \
  --resource-group dns-private-zones \
  --zone-name "<CSV-selected-zone>" \
  --query "[].{name:name,vnet:virtualNetwork.id,registration:registrationEnabled,state:provisioningState,linkState:virtualNetworkLinkState}" \
  --output table
```

Verify the intended remote VNet IDs, successful provisioning, and disabled
auto-registration on newly created links. Existing central links should still
be present.

Run `links what-if` again. Once all required links exist and are healthy, it
should report no additional link creations.

### Verify actual DNS resolution separately

A successful ARM link operation does not prove DNS resolution.

From an authorized client that can reach the non-central resolver's **inbound
endpoint private IP**, query a known existing record in a selected central zone:

```bash
nslookup "<existing-record>.<selected-zone>" "<non-central-inbound-endpoint-ip>"
```

```powershell
Resolve-DnsName -Name '<existing-record>.<selected-zone>' `
  -Server '<non-central-inbound-endpoint-ip>' -DnsOnly
```

Compare the response with the expected private address. Then test through the
normal application/client DNS path, not only a direct endpoint query.

Ensure routes, firewalls, DNS forwarding, resolver endpoints, and records are
already correct. Peering does not make private DNS links transitive. Linking a
resolver VNet does not automatically reconfigure every spoke's DNS settings.
Record migration and application connectivity testing remain separate tasks.

## 8. Configure on-premises DNS forwarding

This is a **separate optional workflow**. First export the central rules, then
preview and apply the saved file across all mapped non-central tenants. Use
`--tenant-id` to limit a run to one destination tenant if needed. It does not
change the private-zone link workflow or create new resolver endpoints.

### Prerequisites and permissions

- Existing healthy DNS Private Resolvers and outbound endpoints. Resolver
  discovery uses the VNet's resource group, as in the existing Lab 03 preflight.
- For creation, Azure CLI's `dns-resolver` extension and the commands used below.
  Check availability with `az dns-resolver forwarding-ruleset create --help`.
- For export, sign in as an authorized central human with read access to the
  central subscription, selected ruleset, and all its forwarding rules.
- For destination setup, sign in as the authorized central human **in each
  non-central guest context**, or as an authorized local human. The script
  checks every selected subscription's tenant and does not sign in automatically
  or create service principals. All-tenant runs require cached CLI access to
  every mapped destination subscription.
- Read access to the subscription's rulesets, resolver endpoints, and child
  resources. Network Contributor or equivalent write permissions are needed on
  the selected ruleset (or its resource group for creation) and resolver VNet.
  No roles or broader Lighthouse delegations are assigned by this workflow.
- Connectivity from the outbound endpoint subnet to each on-premises DNS server,
  including routes/VPN/ExpressRoute and firewalls allowing the configured DNS
  ports over UDP and TCP. The script does not configure or test that connectivity.

### First: export the central forwarding rules

Choose the exact central ruleset ARM ID; the script does not guess between
rulesets. Sign in to the central tenant and export:

```bash
az login --tenant aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa

bash lab-03-cross-tenant-dns-links/scripts/setup-forwarding.sh export \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --source-ruleset-id "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network-central/providers/Microsoft.Network/dnsForwardingRulesets/central-ruleset" \
  --inventory-directory ./.inventory
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Setup-Forwarding.ps1 export `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --source-ruleset-id "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/network-central/providers/Microsoft.Network/dnsForwardingRulesets/central-ruleset" `
  --inventory-directory ./.inventory
```

This writes `./.inventory/lab03-forwarding-rules.json`, following all rule-list
pagination and preserving every supported domain, DNS server IP/port in its
original priority order, and `Enabled`/`Disabled` state. It also records the source
tenant and ruleset IDs. No Azure resources change. Empty source rulesets,
duplicate domains, invalid settings, or failed reads stop export without
replacing an existing file.

Successful exports atomically refresh the output file with owner-only permissions.
Use `--output-file <path>` to save a separate snapshot instead; pass that path as
`--rules-file <path>` when creating destination rules. Ruleset/VNet/outbound
associations and rule names/metadata are not copied: each destination uses its
own mapped resources and deterministic names for new rules.

### Inspect or provide the per-domain rules file

Review the export before applying it. A manually supplied file is also supported:

```json
{
  "schemaVersion": 1,
  "rules": [
    {
      "domainName": "corp.example",
      "forwardingRuleState": "Enabled",
      "targetDnsServers": [
        {"ipAddress": "10.50.0.10", "port": 53},
        {"ipAddress": "10.50.0.11", "port": 53}
      ]
    },
    {
      "domainName": "branch.example.",
      "forwardingRuleState": "Disabled",
      "targetDnsServers": [
        {"ipAddress": "10.60.0.10", "port": 5353}
      ]
    }
  ]
}
```

Replace these with your real on-premises namespaces and reachable DNS servers.
`--rules-file` defaults to `lab03-forwarding-rules.json` under
`--inventory-directory`; the same requested rules apply to each selected
resolver. Domains are lowercased and dot-terminated; single-label domains and
the root wildcard `"."` are supported. Forms such as `"*.example"`, duplicate
domains, invalid IPv4 destinations, and invalid ports are rejected.
`forwardingRuleState` defaults to `Enabled` only when omitted from a manual file;
exported disabled rules stay disabled. Port defaults to 53 when omitted.
Each rule has 1-6 servers; **order is preserved** because the first server is
preferred and subsequent entries are failover destinations.

An enabled root wildcard forwards all matching namespaces. Its DNS servers must
also resolve public names; review its effect on Azure service name resolution
before copying it to other tenants.

If a resolver has multiple outbound endpoints, add `outboundEndpointId` to its
existing resolver-map entry. If multiple existing rulesets qualify, also provide
`forwardingRulesetId`:

```json
{
  "tenantId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
  "subscriptionId": "22222222-2222-2222-2222-222222222222",
  "resolverVnetId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/virtualNetworks/resolver",
  "outboundEndpointId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/dnsResolvers/resolver/outboundEndpoints/outbound",
  "forwardingRulesetId": "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/network-noncentral/providers/Microsoft.Network/dnsForwardingRulesets/existing-ruleset"
}
```

The last two fields are optional selectors, not permission to create or rebind
endpoints. Selected resources must belong to the mapped subscription and the
outbound endpoint must belong to the resolver in the mapped VNet.

### Then: preview and create in all non-central tenants

Sign in to each destination tenant to populate its Azure CLI guest context,
then preview without `--tenant-id` to include **all mapped non-central tenants**:

```bash
# Repeat this login for every mapped destination tenant.
az login --tenant bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb

bash lab-03-cross-tenant-dns-links/scripts/setup-forwarding.sh what-if \
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  --inventory-directory ./.inventory \
  --resolver-map ./.inventory/lab03-resolvers.json
```

```powershell
pwsh -NoProfile -File ./lab-03-cross-tenant-dns-links/scripts/Setup-Forwarding.ps1 what-if `
  --central-tenant-id aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa `
  --inventory-directory ./.inventory `
  --resolver-map ./.inventory/lab03-resolvers.json
```

`validate` and `what-if` are live, read-only script-generated previews. Every
selected resolver is preflighted before any write. The central tenant is
excluded by inventory discovery and cannot be a setup destination.

Replace `what-if` with `create`. Confirmation lists the sorted destination tenant
IDs, comma-separated when more than one tenant is selected; for example:

```text
forwarding:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb,cccccccc-cccc-cccc-cccc-cccccccccccc
```

The workflow:

1. Discovers the resolver's outbound endpoint and existing rulesets/VNet links.
2. Reuses a healthy ruleset already linked to the selected outbound endpoint and
   resolver VNet. Otherwise, it reuses a unique endpoint-associated ruleset and
   adds the missing VNet link, or creates a deterministic ruleset in the endpoint's
   resource group and region.
3. Preserves identical rules, including their enabled/disabled state, even if
   their names differ. Adds only missing domains; different server IPs, ports,
   server order, state mismatches, unsafe
   forwarding loops, name collisions, and ambiguous selections fail preflight.
4. Creates requested rules before adding a new VNet link. Each create uses
   `--if-none-match '*'` so concurrent resources are not overwritten, and each
   result is verified through ARM before continuing.

**Adding rules to a reused ruleset affects every VNet already linked to it.**
No existing rules, VNet links, or endpoints are deleted. The script rejects the
resolver's own inbound IPs as enabled-rule targets when linking its ruleset to that resolver
VNet, and rejects Azure DNS's `168.63.129.16` as a target.

Reruns skip matching rules and links and can finish partial creations. A failure
stops the workflow without automatic rollback; preserve the verified resource IDs
reported in the output. ARM success does not prove forwarding works. From a
client using the resolver's DNS path, query a known on-premises record and compare
the returned address with the expected value. Check that on-premises forwarding
does not send the same namespace back to this resolver and create a loop.

## Reruns and operational limitations

- Use the same reviewed resolver mapping and identity-state file.
- Identity setup reuses the managed identity and skips its matching direct DNS role
  assignment. Do not change the identity name to work around an ambiguous
  match; resolve the ambiguity deliberately.
- Preserve the state file after a partial role-assignment failure. Identity
  creation can also succeed before the first state write; use the progress output
  and Azure to reconcile such failures before retrying.
  If the identity was created and saved but its DNS role assignment failed, rerun
  the same `identities create` command with the same state file to reuse the
  identity and complete the missing assignment. Role lookups use `--scope`
  without the incompatible `--all` flag and skip Graph principal-name lookups.
- A unique existing guest is not reinvited. Check its redemption status manually.
- Matching healthy Lighthouse delegations and central VNet links are preserved.
- `validate` and `what-if` are not offline modes. They query Azure and can fail
  on consent, authorization, policy, or service errors.
- The scripts currently stop immediately on CLI failures; they do not implement
  retry/backoff for propagation or transient errors. Investigate, allow
  propagation where appropriate, and rerun the read-only preview.
- Discovery is limited to the inventories provided and the original exporter's
  resource visibility. It does not establish complete organization-wide coverage.
- Inventory is not an atomic or live snapshot. Refresh it after topology changes.
- Do not run simultaneous identity, delegation, or link workflows against the
  same scopes. Preflight cannot prevent every concurrent change.
- Same-name non-central links to a mapped resolver VNet are replaced only after
  preview and confirmation. Original zones and unrelated links are preserved.
  Other namespace overlaps or conflicts with different central zones fail
  without automatically removing those links.
- Review Azure DNS link limits and organizational policy before applying a large
  zone-by-VNet Cartesian product.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Wrong signed-in tenant | Repeat the explicit login for the current phase; inspect `az account show` |
| Human setup rejects a service principal | Invitation, identity setup, and onboarding are human workflows; use the approved human account |
| Guest invitation forbidden | Check Graph consent, user lookup permissions, invitation rights, external collaboration restrictions, and destination tenant policy |
| Guest exists but cannot deploy | Accept the invitation and obtain separate Azure RBAC permissions; guest membership is not sufficient |
| Ambiguous guest/managed identity | Resolve duplicate matches manually; do not guess an object ID |
| Missing tenant mapping | Map every discovered non-central tenant, or deliberately use a separate inventory input directory |
| VNet absent from inventory | Correct the mapping or refresh the inventory under the intended tenant; do not substitute a similarly named VNet |
| No healthy DNS Private Resolver found | Check the VNet target, resolver provisioning, and the current same-resource-group discovery limitation |
| Provider not registered | Have an authorized administrator review and register `Microsoft.ManagedServices` in that non-central subscription |
| Missing role-assignment permissions | Lighthouse bootstrap requires the documented subscription-level permissions, not only Network Contributor |
| Lighthouse definition conflict | Review managing tenant, principal object IDs, roles, and existing registration; the script will not replace a conflicting delegation |
| Linked authorization failure on link creation | Verify central zone permissions, remote VNet `join/action`, delegation direction, actual caller object ID, tenant/subscription mapping, and propagation |
| Delegated subscription not visible or readable | Check Lighthouse registration, Azure CLI subscription visibility, and the subscription-read preflight requirements; do not automatically grant subscription-wide Contributor |
| Selected zone not found | Finish Lab 02 for that exact central destination or correct the CSV; Lab 03 does not create zones |
| Legacy identity state rejected | Preserve version 1 state for cleanup and run `identities` with a new state-file path |
| Managed identity rejected | Sign in from the Azure host with the configured UAMI; check the token object ID, not the CLI account display name |
| Human tests work but managed identity fails | Human guest RBAC and UAMI Lighthouse authorization are separate paths; verify the UAMI delegation, propagation, and Azure host attachment |
| Link name collision | Review the existing link and target; no overwrite or automatic deletion is performed |
| Missing non-central link delete permission | Obtain approved read/delete access on the exact local zone/link; the resolver-group Lighthouse grant may not cover it |
| Local link changed after preview | Refresh the plan; ETag-conditioned deletion intentionally rejects concurrent changes |
| Local link removed but central creation failed | Use the reported removed-link ID and inventory to restore the local link deliberately, or fix the error and create the central link; verify DNS and any auto-registered records separately |
| Some links created before failure | Keep the progress output, fix the error, run `what-if`, then create only the remaining missing links |
| Links succeeded but DNS fails | Check records, forwarding, endpoint reachability, client DNS settings, and same-name zones; ARM success is not a DNS test |

For persistent cross-tenant authorization failures, stop and verify the exact
supported Azure authorization path with current Microsoft guidance. Do not
silently switch to broader roles, auxiliary credentials, a reversed delegation,
or a different application model.

## Rollback

There is no bulk cleanup command. Rollback must target only reviewed Lab
03-created objects. Existing objects reused by the scripts must be preserved.

The identity-state file records created identity/RBAC IDs. Delegation and link
creation output identifies those changes, but the current implementation does
not maintain a complete durable rollback journal for them. Record the reviewed
plan and successful resource IDs in an approved location, without capturing
credentials from any legacy deployment.

Recommended order:

1. Review which newly added links are no longer needed and their DNS impact.
2. As an authorized central identity, remove only those exact links.
3. As an authorized non-central administrator, remove only the relevant Lab 03
   Lighthouse assignments after confirming no dependent automation needs them.
4. Remove their subscription-scoped registration definitions only after ensuring no assignments
   reference them.
5. Remove only Lab-created role assignments that are no longer required.
6. Delete the managed identity only if this lab created it and no other workload
   depends on it.
7. Review guest removal separately with the destination administrator. A reused
   guest can have unrelated permissions and must not be deleted automatically.

Example removal of **one reviewed link**, not a whole zone:

```bash
az network private-dns link vnet delete \
  --subscription 11111111-1111-1111-1111-111111111111 \
  --resource-group dns-private-zones \
  --zone-name "<reviewed-selected-zone>" \
  --name "<exact-lab03-created-link-name>"
```

Do not delete the private DNS zone or enumerate and delete all its links.

For a legacy deployment, have the application owner separately review and revoke
obsolete application credentials in Entra before removing local credential copies.
Deleting a local credential file does not revoke it. Preserve reused resources
and any application still required by another workload.

For the optional forwarding workflow, remove only the exact rules or VNet links
recorded as created by that run. Delete a newly created ruleset only after
checking that no other VNets or workloads use it. Never delete a reused ruleset
or its pre-existing rules/links as part of Lab 03 rollback.

## Local checks

These commands inspect local syntax/templates and do not authenticate or deploy:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 lab-03-cross-tenant-dns-links/scripts/lab.py --help
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s lab-03-cross-tenant-dns-links/tests
bash -n lab-03-cross-tenant-dns-links/scripts/lab.sh
bash -n lab-03-cross-tenant-dns-links/scripts/setup-guests.sh
bash -n lab-03-cross-tenant-dns-links/scripts/setup-forwarding.sh
PYTHONDONTWRITEBYTECODE=1 python3 lab-03-cross-tenant-dns-links/scripts/forwarding.py --help
az bicep build --file lab-03-cross-tenant-dns-links/lighthouse.bicep --stdout
```

The Bicep CLI must already be available; Azure CLI may otherwise need to download
it. Do not confuse local compilation with Azure validation or a live deployment.
Offline tests cover managed-identity provisioning/reruns, caller guards,
subscription-specific permission queries, delegation authorizations, central-link
preservation, scoped conditional non-central link replacement, and forwarding
ruleset reconciliation/creation. They do not
prove live cross-tenant authorization or DNS resolution.

## References

- [Azure Private DNS FAQ: cross-tenant VNet links](https://learn.microsoft.com/en-us/azure/dns/dns-faq-private)
- [Azure Lighthouse architecture](https://learn.microsoft.com/en-us/azure/lighthouse/concepts/architecture)
- [Onboard a customer to Azure Lighthouse](https://learn.microsoft.com/en-us/azure/lighthouse/how-to/onboard-customer)
- [Lighthouse principals and supported roles](https://learn.microsoft.com/en-us/azure/lighthouse/concepts/tenants-users-roles)
- [Microsoft Graph guest invitations](https://learn.microsoft.com/en-us/graph/api/invitation-post?view=graph-rest-1.0)
- [Interactive Azure CLI login](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-interactively)
- [Managed-identity Azure CLI login](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-managed-identity)
- [Azure CLI private DNS VNet link commands](https://learn.microsoft.com/en-us/cli/azure/network/private-dns/link/vnet?view=azure-cli-latest)
- [Private Resolver endpoints, rulesets, and forwarding behavior](https://learn.microsoft.com/en-us/azure/dns/private-resolver-endpoints-rulesets)

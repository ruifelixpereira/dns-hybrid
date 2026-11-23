# Lab 03 - Cross-tenant resolver VNet links

## Goal and agreed scope

Implement a new `lab-03-cross-tenant-dns-links/` lab that prepares identities and
Azure Lighthouse delegations, then links selected central private DNS zones to
explicitly selected resolver VNets in non-central tenants.

The user selected an implementation plan rather than a reusable prompt alone.
Do not implement repository changes or run Azure operations until approved.

Confirmed decisions:

- Assume the existing central-tenant user is already an accepted guest in the
  other tenants. Retain manual invitation, acceptance, and RBAC instructions.
- Authenticate from the laptop with that central human user and its existing
  guest permissions. Provision only a user-assigned managed identity, without
  deploying an execution host or trying managed-identity login on the laptop.
  Do not create an application, a separate service principal, or client credentials.
- Grant only the managed identity Private DNS Zone Contributor on the explicit
  central private DNS resource group; human permissions are separate prerequisites.
- Discover non-central tenants from Lab 01 inventories in `./.inventory`.
- Require an explicit tenant/subscription/resolver-VNet mapping validated
  against inventory. Show `hubVnetId` only as a reviewable suggestion.
- Link only normalized zone names listed in `hub-dns-zones.csv` and present in
  the explicit central subscription/resource group. Missing selected zones
  fail preflight; Lab 03 does not create zones.
- The user explicitly accepted Network Contributor for the managed identity over
  each resolver VNet's CURRENT RESOURCE GROUP. Lighthouse cannot restrict a
  delegation to an individual VNet. Show the broader scope at confirmation;
  never describe it as VNet-only access.
- Preserve existing central VNet links, zones, unrelated non-central links,
  peerings, resolver settings, private endpoints, and DNS zone groups. Before
  adding a missing central link, preview and confirm removal of same-name
  non-central zone links targeting the exact mapped resolver VNet.
- Lab 01 exports visible A records but Labs 02/03 do not migrate them. Warn that
  deleting registration-enabled links deletes their auto-registered records;
  require operator verification of central records before confirming a swap.

## Azure constraints and validation gate

Microsoft documents cross-tenant private DNS VNet links when the caller has the
necessary permissions on the zone and VNet. Lighthouse delegates subscriptions
or resource groups to principals in a managing tenant.

Before coding the deployment path, verify current Microsoft-supported commands,
role IDs, ManagedServices API versions, managed-identity principal support,
and cross-tenant linked-resource authorization for this exact direction:
central-tenant token writes a central zone link referencing a Lighthouse-delegated
non-central VNet. General cross-tenant support is not proof of a live end-to-end
test. Do not silently substitute multi-tenant applications, additional tenant
credentials, token hacks, subscription-wide access, or a reversed delegation.
Report a concrete blocker and seek a decision if the proposed path is unsupported.

Guest membership is for the human onboarding/test operator, not a Lighthouse
requirement for the managed identity. Guest membership
does not grant Azure RBAC or directory administration.

## Proposed files and approach

Follow Lab 02's thin Bash/PowerShell launchers with shared Python 3.10+
standard-library orchestration and Azure CLI, rather than duplicating logic.

- `lab-03-cross-tenant-dns-links/README.md`: detailed operator runbook.
- `scripts/lab.py`: shared discovery, input validation, Azure CLI invocation,
  identity provisioning, delegation orchestration, and link planning/application.
- `scripts/lab.sh` and `scripts/Lab.ps1`: workflow entry points for discovery,
  central identity setup, tenant delegation, and zone linking.
- `scripts/setup-guests.sh` and `scripts/Setup-Guests.ps1`: dedicated invitation
  setup entry points using the shared implementation, separate from deployment.
- `lighthouse.bicep`: subscription-scoped registration definition and
  resource-group-scoped registration assignment containing only the central
  managed identity object ID and Network Contributor. Use subscription deployment
  commands; never create a subscription-scoped assignment.
- `lighthouse-assignment.bicep`: resource-group module for the assignment,
  referencing the subscription definition.
- `scripts/forwarding.py`, `scripts/setup-forwarding.sh`, and
  `scripts/Setup-Forwarding.ps1`: optional separate per-tenant forwarding setup.
  Accept a versioned per-domain JSON rules file containing IPv4 DNS server
  addresses and ports, preserving server priority. Reuse eligible existing
  rulesets, add missing domains and resolver VNet links, and fail on conflicts.
  Support explicit outboundEndpointId/forwardingRulesetId selectors in the
  resolver map; require selection when discovery is ambiguous. Never create
  endpoints, overwrite existing rules, or rebind rulesets. Preflight all mapped
  resolvers before confirmation, use conditional creates, verify results, and
  reject forwarding through the resolver VNet's own inbound IPs. This setup is
  executed by an authorized human in the non-central tenant; it is not
  automatically run by the existing link workflow.
  Provide an export action for an explicitly selected central ruleset, saving
  a reusable inventory file with ordered DNS servers, ports, domains, and rule
  states. Validate the complete snapshot before atomically refreshing it.
  Setup without --tenant-id processes all mapped non-central tenants, requiring
  their cached human Azure CLI contexts and preflighting every destination
  before confirmation. Do not modify central rulesets during destination setup.
- `tests/test_lab.py` and `tests/mock_az.py`: strict offline tests and CLI double.
- Root `README.md`: add Lab 03 and its relationship to Labs 01 and 02.

Do not commit generated inventories, mappings with real environment data,
credentials, tenant membership information, or Azure state.

## Todos

### 1. Verifying Azure contracts and defining the workflow

- Check authoritative Azure/Graph documentation and the existing repository's
  validation/launcher patterns.
- Define explicit central tenant/subscription/DNS group, identity name/location/
  group, CSV path, inventory directory, and resolver mapping inputs.
- Define a versioned resolver mapping with entries containing `tenantId`,
  `subscriptionId`, and full `resolverVnetId`; allow multiple explicit VNets per
  tenant and deduplicate delegation scopes per subscription/resource group.
- Define read-only/offline planning versus live validation and confirmed apply.
  Only actual ARM deployment previews may be called ARM what-if; identity,
  invitation, RBAC, and CLI link previews must be labeled script-generated.

### 2. Implementing inventory discovery and exact resource scoping

- Discover `*-inventory.json`, validate Lab 01 schema/metadata and ARM IDs, and
  exclude the explicitly supplied central tenant.
- Validate all documents before changes; reject duplicate/conflicting tenants,
  malformed input, uncovered subscriptions, and resolver IDs outside their
  mapped tenant/subscription inventory.
- Require complete explicit resolver mappings for discovered non-central tenants;
  fail on missing/extra mappings rather than silently skipping tenants.
- Treat inventories as stale snapshots, not proof of live permissions or resolver
  deployment. Live preflight verifies tenants, selected VNets, and resolvers;
  fail on a mismatch rather than substituting the hub.
- Read/normalize zone names from the consolidated CSV with its established
  operation consistency checks; deduplicate domains across rows. Do not let
  `none` or `repeated` exclude an otherwise selected existing zone.
- Calculate only the selected-zone-by-explicit-resolver Cartesian product.
  Header-only CSV is an explicit no-op for linking.
- Do not enumerate or change unrelated zones' links.

### 3. Retaining guest-user bootstrap instructions

- Assume guests are already present for normal testing. Keep manual invitation,
  acceptance, destination guest object ID, and administrator RBAC instructions.
  Existing invitation helpers remain optional bootstrap tools.

- Run per destination tenant under an already authenticated local administrator
  or other authorized inviter, using Microsoft Graph invitations via Azure CLI.
  Never assume the not-yet-invited central user can bootstrap its own access.
- Validate the current directory identity/tenant even when no Azure subscriptions
  are available. Do not initiate interactive sign-in automatically.
- Accept the existing central user's invitation address as runtime input.
  Safely identify existing guests; fail on ambiguous matches rather than guessing
  their object ID or deriving a guest UPN from the email.
- Send an invitation only after preview/confirmation; do not reset redemption
  status or resend existing invitations on ordinary reruns.
- Explain manual invitation acceptance and checking the resulting guest object.
  Document Graph consent/Guest Inviter prerequisites and local tenant policy.
- Document separate administrator-granted Azure permissions for onboarding.
  Do not auto-grant Owner or elevate the guest as part of invitation creation.

### 4. Implementing central identity setup and Lighthouse delegation

- Under the central human operator, create/reuse only an unambiguously identified
  user-assigned managed identity.
- Record managed identity client/principal/resource IDs in secret-free version 2
  state. Reject legacy dual-identity state with explicit migration guidance;
  never silently delete Azure resources.
- Assign the managed identity Private DNS Zone Contributor at the explicit central
  DNS resource-group scope, using object IDs and built-in role IDs.
- Do not create credentials or expose secret-generation CLI options. Retain
  owner-only state-file permissions and explicit legacy credential-cleanup guidance.
- The same human signs in explicitly to each non-central tenant after invitation
  redemption and RBAC prerequisites. Delegate each selected resolver resource
  group to the central managing tenant with only the managed identity object ID.
- Check current tenant/subscription, provider registration, and onboarding
  prerequisites. Document Microsoft's subscription-level onboarding permissions,
  including role-assignment read/write/delete, separately from network rights.
- Use deterministic deployment/delegation names and inspect existing definitions;
  fail on conflicting principal/scope/role data instead of widening/replacing it.
- Require confirmation listing every resource-group-wide grant. No automatic
  login, default-subscription changes, or unrequested permission expansion.

### 5. Implementing human and managed-identity link planning and creation

- Document explicit laptop user login in each non-central tenant and then in the
  central tenant, using the existing human account and accepted guest identities.
- Explain that managed identity login is not available on the laptop:
  `az login --identity --client-id <uami-client-id>` requires an Azure execution
  host to which the managed identity is assigned.
- Allow central human-user execution or the configured UAMI only. Check the
  account/token tenant and require a token object ID; for automation, match that
  object ID to the configured UAMI rather than relying on its CLI account name.
  Select remote subscriptions explicitly for guest-context REST queries.
  validate live DNS scope and delegated VNet access before writing links.
- List existing links only on selected central zones. Match targets by normalized
  ARM ID, preserve existing healthy links and registration settings, and fail
  clearly for unhealthy states or unsafe name collisions.
- Create only missing pairs with deterministic names using
  `az network private-dns link vnet create`, explicit central `--subscription`,
  zone/group parameters, full remote VNet ID, and auto-registration disabled.
- Preview each zone/link/remote VNet/tenant before one exact-scope confirmation.
  Preflight all selected targets before writes, stop on the first failure, and
  report partial progress accurately. Use bounded retries only for identified
  transient/propagation conditions.
- Verify created links and provisioning state; do not mistake ARM link success
  for demonstrated DNS resolution or connectivity.

### 6. Adding tests and the step-by-step runbook

- Offline tests: malformed/duplicate inventories and tenant exclusion; missing,
  extra, ambiguous, or mismatched mappings; multi-subscription/multi-VNet targets;
  normalized CSV selection; no link reads/writes for absent-from-CSV zones;
  header-only CSV; guest reruns/tenant guards/consent failures; managed identity
  provisioning/reruns, human/UAMI token guards, and legacy state conflicts; exact RBAC
  and delegation scopes; synthetic previews without writes; confirmation failures;
  existing links; CLI errors and partial failure handling; Bash/PowerShell parity.
- Run the focused unittest suite, Bash syntax checks, PowerShell validation when
  available, and Bicep build using existing tooling. Do not authenticate or
  provision live Azure resources during implementation.
- README order: architecture and permission matrix; prerequisites; inventory and
  explicit resolver review; dedicated guest bootstrap; invitation acceptance and
  administrator RBAC; central managed identity setup; per-tenant Lighthouse
  onboarding; human laptop login or Azure-hosted managed identity login; link
  preview/application; verification; troubleshooting; carefully scoped rollback.
- Human-user tests do not validate the UAMI's delegated authorization. Document
  the separate Azure-hosted test and do not claim offline tests prove live access.
- Explain stale/incomplete inventory, consent/MFA/Conditional Access restrictions,
  propagation delays, insufficient delegation permissions, cross-tenant linked
  authorization errors, resource locks/policies, and same-domain link conflicts.
  Same-name non-central resolver links may be removed only as explicitly
  previewed/confirmed replacements. Use live checks in inventory-covered
  subscriptions, validate the exact zone/VNet scope and delete permissions,
  condition deletions on the link ETag, and verify removal before central
  creation. Skip pairs already linked to the selected central zone. Never delete
  original zones or unrelated links. Report partial removals and DNS interruption
  risks; do not silently roll back or claim exported records have been migrated.
- Explain that link creation does not migrate DNS records, change forwarding or
  ensure application reachability. Include operator-run resolver DNS tests.
- Warn that Lab 02 `create` removes non-central resolver links on selected zones:
  rerunning that cleanup after Lab 03 would undo Lab 03's links. Keep existing
  Lab 02 behavior unchanged; document the sequencing hazard.
- Rollback must target only recorded Lab 03-created objects and require review;
  never bulk-delete shared zones, networks, users, identities, or delegations.

## Dependencies and verification boundary

Todo 1 precedes implementation. Todo 2 is required by invitation discovery,
delegation scope selection, and linking. Todo 3 and central identity provisioning
can be implemented independently after input contracts; delegation deployment at
runtime requires redeemed/authorized operator access. Todo 5 depends on identity
and delegation contracts in todo 4. Todo 6 integrates the completed workflows.

Offline tests prove script planning and command contracts, not actual Azure
cross-tenant execution. The README must mark operator-run live validation steps
as unverified until the operator executes them.

## Authoritative references

- [Private DNS FAQ: cross-tenant VNet links](https://learn.microsoft.com/en-us/azure/dns/dns-faq-private)
- [Lighthouse onboarding, scopes and prerequisites](https://learn.microsoft.com/en-us/azure/lighthouse/how-to/onboard-customer)
- [Lighthouse principals and supported roles](https://learn.microsoft.com/en-us/azure/lighthouse/concepts/tenants-users-roles)
- [Microsoft Graph guest invitations](https://learn.microsoft.com/en-us/graph/api/invitation-post?view=graph-rest-1.0)
- [Service-principal CLI login](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-service-principal)
- [Managed-identity CLI login](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-managed-identity)
- [Private DNS link CLI](https://learn.microsoft.com/en-us/cli/azure/network/private-dns/link/vnet?view=azure-cli-latest)

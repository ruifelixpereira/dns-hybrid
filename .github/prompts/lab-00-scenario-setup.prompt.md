# Azure hybrid DNS lab generation prompt

## Problem and approach

Produce a copy-ready prompt for generating reusable Azure Bicep infrastructure for two independent subscriptions in different tenants. Do not implement or deploy infrastructure in this task.

The workspace contains an empty scenario prompt file and no existing Bicep implementation to reuse. The prompt below incorporates the user's confirmed DNS, simulation, and routing choices.

## Todos

1. Confirm topology, DNS behavior, simulation, and deliverable boundaries. Completed.
2. Verify Azure DNS resolver constraints against Microsoft documentation. Completed.
3. Deliver the copy-ready generation prompt. Ready.

## Copy-ready prompt

```text
Act as a senior Azure networking engineer and Bicep author. Generate a complete, deployable, modular Bicep solution for the following hybrid DNS lab. Generate code and documentation, but do not deploy anything.

TENANT AND DEPLOYMENT MODEL
- Deploy the same architecture independently into Subscription 1 / Tenant A and Subscription 2 / Tenant B.
- Use one reusable subscription-scoped entry point, resource-group-scoped modules, and separate example .bicepparam files.
- Run separate authenticated deployments per tenant/subscription. Do not attempt a single ARM deployment across tenants or assume one identity has access to both.
- No connectivity, DNS links, resource dependencies, or shared resources between tenants.
- Use Azure public cloud. Parameterize region, environment prefix, address spaces, subnet prefixes, VM settings, and tags. Use a single supported region per environment and distinct, nonoverlapping CIDRs across both examples.

NETWORK TOPOLOGY — PER ENVIRONMENT
Create these eight VNets:
1. Hub.
2. Spoke 1, peered with hub.
3. Spoke 2, peered with hub.
4. Test, peered with hub.
5. Private endpoints, peered with hub.
6. Resolver, peered with hub.
7. Standalone, isolated: no peerings, private DNS links, or forwarding-ruleset links.
8. Simulated on-premises, peered directly with hub AND resolver.

Create both directions of every required peering. Do not add other peerings, gateway transit, gateways, firewalls, routing appliances, or routes to simulate transitive peering. The simulated-on-premises VNet is an Azure-hosted simulation, not an actual on-premises connection.

Important: hub peering is NOT transitive. Preserve this topology. Document that spoke/test workloads can resolve private-endpoint names but cannot connect to private endpoints in their sibling VNet through the hub. DNS success must not be presented as data-plane connectivity success.

AZURE DNS PRIVATE RESOLVER
- Deploy one Azure DNS Private Resolver in the resolver VNet.
- Create separate dedicated inbound and outbound subnets, each /28 or larger within supported service limits, delegated to Microsoft.Network/dnsResolvers. Place no unrelated resources in those subnets.
- Deploy inbound and outbound endpoints. Prefer a parameterized valid static inbound IP, excluding Azure-reserved subnet addresses.
- Create a DNS forwarding ruleset associated with the outbound endpoint.
- Link the ruleset to hub, spoke 1, spoke 2, test, private-endpoints, and resolver VNets.
- Add an enabled rule for the exact fake namespace demo-lab. (including trailing dot), forwarding to the simulated Windows DNS server's static private IP on port 53.
- Do not link this ruleset to standalone or simulated-on-premises VNets.
- Do not add wildcard or private-zone forwarding rules back to the resolver inbound endpoint. Avoid DNS forwarding loops.

PRIVATE DNS AND PRIVATE ENDPOINTS
- In each subscription, create a resource group named exactly dns-private-zones.
- Put both private DNS zones in that resource group:
  - privatelink.blob.core.windows.net
  - privatelink.vaultcore.azure.net
- Link both zones to hub, spoke 1, spoke 2, test, private-endpoints, and resolver VNets, with auto-registration disabled.
- Keep these six VNets on Azure-provided DNS. Do not configure resolver inbound as their custom DNS server.
- Do not link either zone to standalone or simulated-on-premises VNets.
- Create one StorageV2 account and a blob private endpoint, using subresource groupId blob.
- Create one Key Vault and its private endpoint, using subresource groupId vault.
- Allocate ALL private endpoints from a dedicated subnet in the private-endpoints VNet, never from resolver subnets.
- Attach each endpoint to its matching private DNS zone through a private DNS zone group. Do not hand-author duplicate endpoint A records.
- Disable public network access on Storage and Key Vault. Disable anonymous blob access, require HTTPS and supported secure TLS, and use Key Vault RBAC authorization.
- Use globally unique compliant Storage and Key Vault names, deterministically derived from deployment inputs.
- Do not create sample secrets, credentials in Storage, or unnecessary role assignments. Explain that private networking does not grant data-plane authorization.

WORKING SIMULATED ON-PREMISES DNS
- Deploy one standalone Windows Server VM per environment in a dedicated subnet of the simulated-on-premises VNet; no Active Directory/domain controller.
- Parameterize a supported image and appropriately sized VM SKU. Use a static private NIC IP.
- No public IP, internet-exposed RDP, Bastion, or additional test VMs by default. Use Azure VM Run Command for configuration/validation.
- Accept the administrator password only through a secure parameter and runtime secret injection. Never put passwords in checked-in example parameter files, commands, outputs, scripts, or logs.
- Install and configure the Windows DNS role using repeatable PowerShell delivered through an appropriate Bicep-managed extension or managed Run Command resource.
- Host an authoritative demo-lab zone and a deterministic sample record, such as dns-test.demo-lab pointing to the VM's private IP. Make configuration idempotent and fail explicitly on errors.
- Configure Windows DNS conditional forwarders for blob.core.windows.net and vault.azure.net to the Azure resolver inbound IP, so normal service names and their private-link aliases resolve through Azure.
- Ensure Windows DNS answers demo-lab authoritatively instead of forwarding it back to Azure.
- Permit DNS TCP/UDP 53 only from the required resolver subnet ranges and explicitly identified lab clients in both NSGs and Windows Firewall. Do not expose an open recursive resolver.
- Account explicitly for current Azure outbound-access defaults. If bootstrap or VM-agent operation requires outbound connectivity, provide the minimum documented egress path; do not assume implicit default outbound access. Explain any additional cost.

ENGINEERING REQUIREMENTS
- Use current supported stable API versions and verify schemas against official Microsoft documentation. Respect regional availability and resolver/ruleset constraints.
- Use clear modules for networking, DNS, private-link services, and Windows DNS simulation; avoid unnecessary abstraction.
- Use separate resource groups for logical workloads where useful; only dns-private-zones has a mandated exact name.
- Use symbolic references/module outputs to establish dependencies. Handle cross-resource-group scoping correctly.
- Keep deployments repeatable. Do not use manual portal configuration, hard-coded subscription/tenant IDs, secret defaults, or success-shaped error handling.
- Output useful nonsecret IDs, endpoint IPs, service FQDNs, DNS VM identity, and sample DNS record details.

DELIVERABLES AND VALIDATION
1. Show the proposed file tree and full contents of every required file: Bicep modules, two example .bicepparam files, Windows DNS bootstrap script, and README.
2. Include a Mermaid topology diagram, CIDR/subnet table, and concise explanation of both DNS flows:
   - Azure-provided DNS -> linked private zones for private endpoints.
   - Azure-provided DNS -> linked ruleset -> resolver outbound -> Windows DNS for demo-lab.
   Also explain Windows DNS -> resolver inbound -> private-zone resolution.
3. Provide Azure CLI prerequisites, provider registrations, permissions, and separate login/account-selection, build/lint, validation, what-if, and deployment commands for each tenant. Securely prompt for secrets at runtime without echoing or persisting them. Deployment must require an explicit operator action.
4. Provide executable DNS checks through VM Run Command, including:
   - Query resolver inbound for dns-test.demo-lab and expect the configured sample IP.
   - Query Azure-provided DNS from the DNS VM for dns-test.demo-lab as a negative control; its VNet has no ruleset link.
   - Query the Windows DNS service for normal blob and Key Vault service FQDNs and expect the corresponding private-endpoint IPs through conditional forwarding.
   - Verify resource configuration for private-zone links, ruleset links, peerings, and the isolated VNet.
   Label tests requiring a client in another VNet as operator-run tests; do not claim those ran or create extra VMs silently.
5. Distinguish DNS resolution, TCP reachability, and authorized service access. Explain expected routing failures from spokes/test to sibling-VNet private endpoints, and that standalone isolation means no private links/routes—not guaranteed NXDOMAIN for public service names.
6. Include troubleshooting, recurring-cost warnings (especially Private Resolver, Windows VM/disks, and any egress resources), and explicit tenant-scoped cleanup commands limited to this lab's resource groups.
7. Run available local Bicep build/lint checks and report exact outcomes. If tools or Azure access are unavailable, state what was not verified. Never claim deployment or live DNS tests succeeded without executing them.

Before generating code, briefly summarize the architecture and identify any genuine blocker. Preserve the explicit design choices above; do not silently add transitive routing or cross-tenant integration.
```

## Verified references

- [Azure DNS Private Resolver endpoints and rulesets](https://learn.microsoft.com/en-us/azure/dns/private-resolver-endpoints-rulesets): ruleset links, same-region requirement, trailing-dot namespaces, single-label support, dedicated endpoint subnets, and loop avoidance.
- [Azure Private Endpoint DNS integration](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns-integration): Azure-provided DNS and private-zone resolution.

## Notes

This task creates only the prompt. Any future implementation, Azure authentication, deployment, paid resources, or live validation is outside the current scope.

# Azure hybrid DNS labs

Hands-on labs for Azure hybrid DNS across independent subscriptions and tenants.

## Labs

| Lab | Description |
| --- | --- |
| [Lab 00 - Scenario setup](./lab-00-scenario-setup/README.md) | Deploy the baseline two-tenant networking, Private Resolver, private endpoints, and Windows DNS simulation. |
| [Lab 01 - Inventory](./lab-01-inventory/README.md) | Export tenant-scoped private DNS and VNet inventories, then report explicit zone links to a hub and its direct peers. |
| [Lab 02 - Central private DNS zones](./lab-02-central-dns-zones/README.md) | Create missing central private DNS zones and keep only resolver VNet links for CSV-listed destination-group zones. |
| [Lab 04 - VNet DNS alignment](./lab-04-vnet-dns-alignment/README.md) | Point inventoried hubs/direct spokes to their tenant's resolver inbound DNS and keep resolver VNets on Azure-provided DNS. |

Each lab contains its own infrastructure, parameters, scripts, tests, and instructions.
Repository-wide configuration stays at the root.

## Getting started

```bash
cd lab-00-scenario-setup
```

Follow the [Lab 00 instructions](./lab-00-scenario-setup/README.md) for prerequisites,
validation, deployment, and cleanup. Deployment creates billable Azure resources
and requires an explicit operator action; nothing deploys automatically.

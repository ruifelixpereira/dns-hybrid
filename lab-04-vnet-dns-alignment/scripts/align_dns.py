#!/usr/bin/env python3
"""Align inventoried hub/direct-spoke VNet DNS with each tenant's Private Resolver."""

import argparse
import importlib.util
import ipaddress
from pathlib import Path
import re
import sys
from uuid import uuid4


REPOSITORY = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "lab04_common", REPOSITORY / "lab-03-cross-tenant-dns-links/scripts/lab.py",
)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
DNS_API = "2022-07-01"


def vnet_parts(resource_id):
    return common.inventory.identity({"name": "VNet", "id": resource_id}, common.inventory.VNET, "VNet")


def topology(document, resolver_id):
    networks = {common.normalized(vnet["id"]): vnet for vnet in document["virtualNetworks"]}
    hub_id = common.field(document, "hubVnetId", "inventory")
    hub_key = common.normalized(hub_id)
    resolver_key = common.normalized(resolver_id)
    vnet_parts(hub_id)
    if hub_key not in networks:
        raise ValueError(f"Inventory hubVnetId is not an inventoried VNet: {hub_id}")
    if resolver_key not in networks:
        raise ValueError(f"Resolver VNet is not in its tenant inventory: {resolver_id}")
    if hub_key == resolver_key:
        raise ValueError("Hub and resolver VNets must differ: hub uses inbound DNS; resolver uses Azure-provided DNS")
    peers = set()
    for key, vnet in networks.items():
        for peering in vnet["peerings"]:
            remote = common.normalized(peering["remoteVirtualNetwork"]["id"])
            if key == hub_key:
                peers.add(remote)
            elif remote == hub_key:
                peers.add(key)
    spokes = [networks[key] for key in sorted(peers & networks.keys()) if key not in (hub_key, resolver_key)]
    return networks[hub_key], networks[resolver_key], spokes


def endpoint_ips(endpoint, resolver_id):
    value = common.object_value(endpoint.get("properties"), "inbound endpoint properties")
    if value.get("provisioningState") != "Succeeded":
        raise ValueError(f"Inbound endpoint is not healthy: {common.field(endpoint, 'id', 'inbound endpoint')}")
    addresses = []
    for config in common.collection(value.get("ipConfigurations"), "inbound IP configurations"):
        subnet = common.field(config.get("subnet"), "id", "inbound subnet")
        if not common.normalized(subnet).startswith(common.normalized(resolver_id) + "/subnets/"):
            raise ValueError(f"Inbound endpoint subnet is outside the mapped resolver VNet: {subnet}")
        address = ipaddress.IPv4Address(common.field(config, "privateIpAddress", "inbound IP configuration"))
        if (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local
                or str(address) == "168.63.129.16"):
            raise ValueError(f"Invalid inbound endpoint IP: {address}")
        if str(address) in addresses:
            raise ValueError(f"Duplicate inbound endpoint IP: {address}")
        addresses.append(str(address))
    if not addresses:
        raise ValueError("Inbound endpoint has no IPv4 addresses")
    return addresses


def inbound_endpoint(target, options):
    subscription = target["subscriptionId"]
    base = f"{common.ARM}{common.group_scope(subscription, target['resourceGroup'])}"
    resolvers = common.pages(f"{base}/providers/Microsoft.Network/dnsResolvers?api-version={DNS_API}",
                             subscription=subscription)
    endpoints = []
    for resolver in resolvers:
        value = common.object_value(resolver.get("properties"), "DNS resolver properties")
        if common.normalized(common.field(value.get("virtualNetwork"), "id", "DNS resolver VNet")) != common.normalized(target["resolverVnetId"]):
            continue
        resolver_id = common.field(resolver, "id", "DNS resolver")
        expected = re.escape(common.group_scope(subscription, target["resourceGroup"]))
        common.inventory.identity(resolver, expected + r"/providers/Microsoft.Network/dnsResolvers/[^/]+",
                                  "DNS resolver")
        if value.get("provisioningState") != "Succeeded":
            raise ValueError(f"DNS resolver is not healthy: {resolver_id}")
        for endpoint in common.pages(f"{common.ARM}{resolver_id}/inboundEndpoints?api-version={DNS_API}",
                                     subscription=subscription):
            common.inventory.identity(endpoint, re.escape(resolver_id.rstrip("/")) + r"/inboundEndpoints/[^/]+",
                                      "inbound endpoint")
            addresses = endpoint_ips(endpoint, target["resolverVnetId"])
            endpoints.append({"id": endpoint["id"], "dns": addresses})
    selected_id = options.get("inboundEndpointId")
    if selected_id is not None:
        selected_id = common.text(selected_id, "inboundEndpointId")
        endpoints = [endpoint for endpoint in endpoints if common.normalized(endpoint["id"]) == common.normalized(selected_id)]
    if len(endpoints) != 1:
        raise ValueError(f"Expected one inbound endpoint for {target['resolverVnetId']}; "
                         "specify inboundEndpointId in its resolver-map entry when selection is ambiguous")
    return endpoints[0]


def read_vnet(resource_id):
    subscription = common.guid(vnet_parts(resource_id)["subscription"], "VNet subscription")
    resource = common.az("network", "vnet", "show", "--subscription", subscription, "--ids", resource_id)
    if common.normalized(common.field(resource, "id", "live VNet")) != common.normalized(resource_id):
        raise ValueError(f"VNet lookup returned an unexpected ID: {resource_id}")
    if resource.get("provisioningState") != "Succeeded":
        raise ValueError(f"VNet is not healthy: {resource_id}")
    options = resource.get("dhcpOptions")
    values = common.object_value(options, "VNet dhcpOptions").get("dnsServers") if options is not None else None
    dns = [str(ipaddress.ip_address(common.text(value, "VNet DNS server")))
           for value in common.collection(values, "VNet DNS servers")] if values is not None else []
    return dns, common.field(resource, "etag", "live VNet")


def live_peers(resource_id):
    match = vnet_parts(resource_id)
    peerings = common.collection(common.az(
        "network", "vnet", "peering", "list", "--subscription", match["subscription"],
        "--resource-group", match["group"], "--vnet-name", resource_id.rstrip("/").rsplit("/", 1)[-1],
    ), "live VNet peerings")
    result = set()
    for peering in peerings:
        remote = common.field(peering.get("remoteVirtualNetwork"), "id", "live peering remote VNet")
        vnet_parts(remote)
        result.add(common.normalized(remote))
    return result


def plan_tenant(target, options, document):
    hub, resolver, spokes = topology(document, target["resolverVnetId"])
    endpoint = inbound_endpoint(target, options)
    hub_peers = live_peers(hub["id"]) if spokes else set()
    for spoke in spokes:
        if (common.normalized(spoke["id"]) not in hub_peers
                and common.normalized(hub["id"]) not in live_peers(spoke["id"])):
            raise ValueError(f"Inventory hub peering is no longer present live: {spoke['id']}; refresh the inventory")
    rows = []
    for role, network in [("resolver", resolver), ("hub", hub), *[("spoke", spoke) for spoke in spokes]]:
        current, etag = read_vnet(network["id"])
        desired = [] if role == "resolver" else endpoint["dns"]
        changed = current != desired
        if changed:
            common.permissions(network["id"], ["Microsoft.Network/virtualNetworks/write"],
                               subscription=network["subscriptionId"])
        rows.append({
            "tenantId": target["tenantId"], "id": network["id"], "role": role,
            "subscriptionId": network["subscriptionId"], "dnsBefore": current, "dnsAfter": desired,
            "etag": etag, "changed": changed, "status": "planned",
            "endpointId": endpoint["id"], "resolverVnetId": resolver["id"],
        })
    return rows


def apply(changes, directory, tenants):
    for endpoint_id in sorted({row["endpointId"] for row in changes if row["role"] != "resolver"}):
        row = next(row for row in changes if row["endpointId"] == endpoint_id and row["role"] != "resolver")
        subscription = common.guid(vnet_parts(row["resolverVnetId"])["subscription"], "resolver subscription")
        endpoint = common.az("rest", "--method", "get", "--url",
                             f"{common.ARM}{endpoint_id}?api-version={DNS_API}", "--subscription", subscription)
        if common.normalized(common.field(endpoint, "id", "inbound endpoint")) != common.normalized(endpoint_id):
            raise ValueError("Inbound endpoint lookup returned a different resource")
        if endpoint_ips(endpoint, row["resolverVnetId"]) != row["dnsAfter"]:
            raise ValueError(f"Inbound endpoint IP changed after preview: {endpoint_id}; rerun validation")
    backup = directory / f"lab04-dns-backup-{uuid4()}.json"
    state = {"schemaVersion": 1, "tenantIds": tenants, "changes": changes}
    common.save_state(backup, state)
    print(f"DNS settings backup: {backup}", flush=True)
    for row in changes:
        current, etag = read_vnet(row["id"])
        if current != row["dnsBefore"] or etag != row["etag"]:
            raise ValueError(f"VNet changed after preview: {row['id']}; rerun validation before updating DNS")
        row["status"] = "updating"
        common.save_state(backup, state)
        arguments = row["dnsAfter"] if row["dnsAfter"] else ["null"]
        common.az("network", "vnet", "update", "--subscription", row["subscriptionId"],
                  "--ids", row["id"], "--dns-servers", *arguments)
        actual, _ = read_vnet(row["id"])
        if actual != row["dnsAfter"]:
            raise ValueError(f"VNet DNS verification failed: {row['id']}")
        row["status"] = "verified"
        common.save_state(backup, state)
        print(f"Updated and verified {row['role']} VNet DNS: {row['id']} -> {actual or 'Azure-provided DNS'}",
              flush=True)


def generate_map(args, documents):
    if args.central_tenant_id not in documents:
        raise ValueError("The central tenant must have an inventory in --inventory-directory")
    source = args.source_resolver_map or args.inventory_directory / "lab03-resolvers.json"
    output = args.output_file or args.inventory_directory / "lab04-resolvers.json"
    if source.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the Lab 03 source resolver map")
    if output.exists() and not args.overwrite:
        raise ValueError(f"Output already exists: {output}; use --overwrite to replace it")
    document = common.object_value(common.read_json(source), "Lab 03 resolver mapping")
    noncentral = {tenant: value for tenant, value in documents.items() if tenant != args.central_tenant_id}
    common.validate_resolvers(document, noncentral)
    central = {
        "tenantId": args.central_tenant_id,
        "subscriptionId": common.guid(vnet_parts(args.central_resolver_vnet_id)["subscription"],
                                      "central resolver subscription"),
        "resolverVnetId": args.central_resolver_vnet_id,
    }
    if args.central_inbound_endpoint_id is not None:
        central["inboundEndpointId"] = args.central_inbound_endpoint_id
    generated = {**document, "resolvers": [*document["resolvers"], central]}
    targets = common.validate_resolvers(generated, documents)
    if len(targets) != len(documents):
        raise ValueError("Lab 04 requires exactly one resolver VNet per inventoried tenant")
    options = {common.normalized(entry["resolverVnetId"]): entry for entry in generated["resolvers"]}
    for target in targets:
        topology(documents[target["tenantId"]], target["resolverVnetId"])
        endpoint_id = options[common.normalized(target["resolverVnetId"])].get("inboundEndpointId")
        if endpoint_id is not None:
            scope = common.group_scope(target["subscriptionId"], target["resourceGroup"])
            common.inventory.identity(
                {"name": "inbound endpoint", "id": endpoint_id},
                re.escape(scope) + r"/providers/Microsoft.Network/dnsResolvers/[^/]+/inboundEndpoints/[^/]+",
                "inboundEndpointId",
            )
    common.save_state(output, generated)
    print(f"Generated resolver map for {len(targets)} tenants: {output}")
    print("Offline inventory validation only. Run what-if to verify live resolvers, endpoints, and permissions.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate-map", "validate", "what-if", "create"))
    parser.add_argument("--inventory-directory", type=Path, default=Path("./.inventory"))
    parser.add_argument("--resolver-map", type=Path, help="Required for validate, what-if, and create.")
    parser.add_argument("--tenant-id", type=lambda value: common.guid(value, "tenant"),
                        help="Limit updates to one tenant; default is every inventoried tenant.")
    parser.add_argument("--source-resolver-map", type=Path,
                        help="generate-map input; default: <inventory-directory>/lab03-resolvers.json.")
    parser.add_argument("--output-file", type=Path,
                        help="generate-map output; default: <inventory-directory>/lab04-resolvers.json.")
    parser.add_argument("--central-tenant-id", type=lambda value: common.guid(value, "central tenant"))
    parser.add_argument("--central-resolver-vnet-id", help="Full ARM VNet ID; its subscription ID is derived.")
    parser.add_argument("--central-inbound-endpoint-id", help="Optional full ARM inbound endpoint ID.")
    parser.add_argument("--overwrite", action="store_true", help="Allow generate-map to replace an existing output.")
    args = parser.parse_args(argv)
    if args.action == "generate-map":
        if not args.central_tenant_id or not args.central_resolver_vnet_id:
            parser.error("generate-map requires --central-tenant-id and --central-resolver-vnet-id")
        if args.resolver_map is not None or args.tenant_id is not None:
            parser.error("generate-map does not accept --resolver-map or --tenant-id")
    else:
        if args.resolver_map is None:
            parser.error(f"{args.action} requires --resolver-map")
        if any(value is not None for value in (
            args.source_resolver_map, args.output_file, args.central_tenant_id,
            args.central_resolver_vnet_id, args.central_inbound_endpoint_id,
        )) or args.overwrite:
            parser.error("Map generation options are only valid with generate-map")
    try:
        documents = common.discover(args.inventory_directory)
        if args.action == "generate-map":
            generate_map(args, documents)
            return 0
        targets = common.resolver_targets(args.resolver_map, documents)
        if any(sum(target["tenantId"] == tenant for target in targets) != 1 for tenant in documents):
            raise ValueError("Lab 04 requires exactly one resolver VNet per inventoried tenant")
        selected = [target for target in targets if args.tenant_id is None or target["tenantId"] == args.tenant_id]
        if not selected:
            raise ValueError("No inventoried tenant matches --tenant-id")
        options = {common.normalized(entry["resolverVnetId"]): entry
                   for entry in common.read_json(args.resolver_map)["resolvers"]}
        subscriptions = set()
        for target in selected:
            hub, resolver, spokes = topology(documents[target["tenantId"]], target["resolverVnetId"])
            subscriptions.update((target["tenantId"], network["subscriptionId"]) for network in [hub, resolver, *spokes])
        for tenant, subscription in sorted(subscriptions):
            common.current_account(tenant, subscription, human=True)
            common.live_subscription(subscription, tenant)
        rows = [row for target in selected for row in plan_tenant(
            target, options[common.normalized(target["resolverVnetId"])], documents[target["tenantId"]],
        )]
        tenants = sorted({target["tenantId"] for target in selected})
        print("Script-generated VNet DNS preview (not ARM what-if):")
        print(f"  Tenants: {', '.join(tenants)}")
        for row in rows:
            print(f"  {'Update' if row['changed'] else 'Keep'} {row['role']}: {row['id']} "
                  f"{row['dnsBefore'] or 'Azure-provided DNS'} -> {row['dnsAfter'] or 'Azure-provided DNS'}")
        changes = [row for row in rows if row["changed"]]
        print(f"  {len(changes)} DNS configurations to replace; no VNets or peerings to create/delete.")
        print("WARNING: Inventory-listed peering states are not filtered. Verify routing and UDP/TCP 53 reachability "
              "to each inbound endpoint. Peering is not transitive. Clients may require DHCP renewal or restart; "
              "NIC/guest DNS overrides are not changed.")
        if args.action == "create" and changes:
            common.confirm("vnet-dns", ",".join(tenants))
            apply(changes, args.inventory_directory, tenants)
    except (OSError, ValueError, EOFError) as exc:
        print(f"Error: {exc or 'confirmation input ended'}", file=sys.stderr)
        print("Stopped. Earlier successful DNS updates are not rolled back; inspect Azure and any printed backup.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

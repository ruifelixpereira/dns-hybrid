#!/usr/bin/env python3
"""Discover central private DNS zones linked to a selected resolver VNet."""

import argparse
from datetime import datetime, timezone
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "lab05_alignment", REPOSITORY / "lab-04-vnet-dns-alignment/scripts/align_dns.py",
)
alignment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alignment)
common = alignment.common
ZONE_API = "2018-09-01"


def inbound_ips(values):
    addresses = []
    for value in values:
        address = ipaddress.IPv4Address(common.text(value, "inbound IP"))
        first, second, *_ = address.packed
        if (first in (0, 127) or first >= 224 or (first == 169 and second == 254)
                or str(address) == "168.63.129.16" or str(address) in addresses):
            raise ValueError(f"Invalid or duplicate inbound IPv4 address: {value}")
        addresses.append(str(address))
    if not addresses:
        raise ValueError("Specify at least one inbound IPv4 address")
    return addresses


def collect(tenant, resolver_id, supplied_ips):
    servers = inbound_ips(supplied_ips)
    parts = alignment.vnet_parts(resolver_id)
    resolver_subscription = common.guid(parts["subscription"], "resolver subscription")
    accounts = common.collection(common.az("account", "list", "--all"), "Azure accounts")
    subscriptions = sorted({
        common.guid(common.field(account, "id", "account"), "account subscription")
        for account in accounts
        if common.guid(common.field(account, "tenantId", "account"), "account tenant") == tenant
        and account.get("state") == "Enabled"
    })
    if resolver_subscription not in subscriptions:
        raise ValueError("Resolver subscription is not an accessible Enabled subscription in the central tenant")
    for subscription in subscriptions:
        common.current_account(tenant, subscription, human=True)
        common.live_subscription(subscription, tenant)
    alignment.read_vnet(resolver_id)
    selected = {}
    seen = set()
    for subscription in subscriptions:
        zones = common.pages(
            f"{common.ARM}/subscriptions/{subscription}/providers/Microsoft.Network/privateDnsZones"
            f"?api-version={ZONE_API}", subscription=subscription,
        )
        for zone in zones:
            zone_parts = common.inventory.identity(zone, common.inventory.ZONE, "private DNS zone")
            if common.guid(zone_parts["subscription"], "zone subscription") != subscription:
                raise ValueError("Private DNS zone list returned a zone outside the requested subscription")
            zone_id = common.field(zone, "id", "private DNS zone")
            key = common.normalized(zone_id)
            if key in seen:
                raise ValueError(f"Duplicate private DNS zone ID returned: {zone_id}")
            seen.add(key)
            name = common.central.zone_name(common.field(zone, "name", "private DNS zone"), "zone")
            if zone_id.rstrip("/").rsplit("/", 1)[-1].casefold() != name:
                raise ValueError(f"Private DNS zone name disagrees with its resource ID: {zone_id}")
            links = common.pages(f"{common.ARM}{zone_id}/virtualNetworkLinks?api-version={ZONE_API}",
                                 subscription=subscription)
            matching = []
            for link in links:
                common.inventory.identity(link, re.escape(zone_id.rstrip("/")) + r"/virtualNetworkLinks/[^/]+",
                                          "private DNS link")
                properties = common.object_value(link.get("properties"), "private DNS link properties")
                remote = common.field(properties.get("virtualNetwork"), "id", "linked VNet")
                alignment.vnet_parts(remote)
                if common.normalized(remote) == common.normalized(resolver_id):
                    matching.append(properties)
            if not matching:
                continue
            if (len(matching) != 1 or matching[0].get("provisioningState") != "Succeeded"
                    or matching[0].get("virtualNetworkLinkState") != "Completed"):
                raise ValueError(f"Resolver VNet link is ambiguous or unhealthy: {zone_id}")
            if name in selected:
                raise ValueError(f"Multiple central private DNS zones with the same linked namespace: {name}")
            if common.object_value(zone.get("properties"), "private DNS zone properties").get("provisioningState") != "Succeeded":
                raise ValueError(f"Private DNS zone is not healthy: {zone_id}")
            selected[name] = {"name": name, "id": zone_id, "subscriptionId": subscription}
    return {
        "schemaVersion": 2, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "centralTenantId": tenant, "resolverVnetId": resolver_id,
        "subscriptionIds": subscriptions, "dnsServers": servers,
        "zones": [selected[name] for name in sorted(selected)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("discover", "export"))
    parser.add_argument("--central-tenant-id", type=lambda value: common.guid(value, "central tenant"), required=True)
    parser.add_argument("--resolver-vnet-id", required=True)
    parser.add_argument("--inbound-ip", nargs="+", required=True,
                        help="Inbound endpoint IPv4 address(es), in preferred forwarding order; no endpoint lookup.")
    parser.add_argument("--output-file", type=Path, default=Path("./.inventory/lab05-conditional-forwarders.json"))
    args = parser.parse_args(argv)
    try:
        document = collect(args.central_tenant_id, args.resolver_vnet_id, args.inbound_ip)
        if args.action == "export":
            common.save_state(args.output_file, document)
            print(f"Exported {len(document['zones'])} linked private DNS zones: {args.output_file}")
        else:
            print(json.dumps(document))
    except (OSError, ValueError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

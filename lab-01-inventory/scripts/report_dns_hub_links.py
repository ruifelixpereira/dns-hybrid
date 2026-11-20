#!/usr/bin/env python3
"""Validate Azure inventory and report explicit hub/direct-peer DNS links."""

import argparse
import csv
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import UUID


COLUMNS = [
    "privateDnsZoneName", "privateDnsZoneId", "privateDnsZoneSubscriptionId",
    "privateDnsZoneResourceGroup", "virtualNetworkLinkName", "virtualNetworkLinkId",
    "linkedVnetName", "linkedVnetId", "relationshipToHub", "hubVnetName", "hubVnetId",
    "tenantId",
]
ARM_PREFIX = (
    r"/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<group>[^/]+)"
    r"/providers/Microsoft\.Network/"
)
VNET = ARM_PREFIX + r"virtualNetworks/[^/]+"
ZONE = ARM_PREFIX + r"privateDnsZones/[^/]+"


def normalized_id(value):
    return value.rstrip("/").casefold()


def object_value(value, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected an object")
    return value


def array(value, context):
    if not isinstance(value, list):
        raise ValueError(f"{context}: expected an array")
    return value


def text(value, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected a nonempty string")
    return value


def guid(value, context):
    value = text(value, context)
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError(f"{context}: expected a GUID")
    return str(UUID(value))


def boolean(value, context, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not bool:
        raise ValueError(f"{context}: expected a boolean")


def required(obj, fields, context):
    object_value(obj, context)
    for field in fields:
        if field not in obj:
            raise ValueError(f"{context}: missing {field}")


def identity(value, pattern, context):
    required(value, ("name", "id"), context)
    text(value["name"], f"{context}.name")
    resource_id = text(value["id"], f"{context}.id")
    match = re.fullmatch(pattern + r"/*", resource_id, re.IGNORECASE)
    if not match:
        raise ValueError(f"{context}: invalid ARM resource ID: {resource_id}")
    if "subscription" in match.groupdict():
        guid(match["subscription"], f"{context}.id subscription")
    return match


def nullable_fields(value, fields, context, is_boolean=False):
    required(value, fields, context)
    for field in fields:
        item = value[field]
        if is_boolean:
            boolean(item, f"{context}.{field}", nullable=True)
        elif item is not None:
            text(item, f"{context}.{field}")


def utc_timestamp(value, context):
    value = text(value, context)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{context}: invalid ISO 8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError(f"{context}: timestamp must be UTC")
    return result


def validate_envelope(document, collection):
    required(document, (
        "schemaVersion", "inventoryId", "tenantId", "collectionStartedAt",
        "collectionCompletedAt", "subscriptions", "excludedSubscriptions", collection,
    ), collection)
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise ValueError(f"{collection}: unsupported schemaVersion")
    text(document["inventoryId"], "inventoryId")
    guid(document["tenantId"], "tenantId")
    started = utc_timestamp(document["collectionStartedAt"], "collectionStartedAt")
    ended = utc_timestamp(document["collectionCompletedAt"], "collectionCompletedAt")
    if ended < started:
        raise ValueError("collectionCompletedAt precedes collectionStartedAt")
    seen = set()
    enabled = set()
    for key in ("subscriptions", "excludedSubscriptions"):
        for item in array(document[key], key):
            required(item, ("name", "id", "state"), key)
            text(item["name"], f"{key}.name")
            sub = guid(item["id"], f"{key}.id")
            state = text(item["state"], f"{key}.state")
            if sub in seen:
                raise ValueError(f"Duplicate subscription coverage: {sub}")
            seen.add(sub)
            if key == "subscriptions":
                if state != "Enabled":
                    raise ValueError("subscriptions must contain only Enabled subscriptions")
                enabled.add(sub)
            else:
                if state == "Enabled":
                    raise ValueError("excludedSubscriptions cannot contain Enabled subscriptions")
                text(item.get("reason"), "excludedSubscriptions.reason")
    if not enabled:
        raise ValueError("No enabled subscriptions in inventory")
    array(document[collection], collection)
    return enabled


def validate_resource(resource, pattern, subscriptions, context):
    parts = identity(resource, pattern, context)
    required(resource, ("subscriptionId", "resourceGroup"), context)
    subscription = guid(resource["subscriptionId"], f"{context}.subscriptionId")
    if subscription != str(UUID(parts["subscription"])) or subscription not in subscriptions:
        raise ValueError(f"{context}: resource subscription is outside inventory coverage")
    if text(resource["resourceGroup"], f"{context}.resourceGroup").casefold() != parts["group"].casefold():
        raise ValueError(f"{context}: resource group disagrees with ARM ID")


def validate_documents(dns, networks):
    dns_subs = validate_envelope(dns, "privateDnsZones")
    net_subs = validate_envelope(networks, "virtualNetworks")
    for key in ("schemaVersion", "inventoryId", "collectionStartedAt", "collectionCompletedAt"):
        if dns[key] != networks[key]:
            raise ValueError(f"Inventory files have mismatched {key}")
    if guid(dns["tenantId"], "tenantId") != guid(networks["tenantId"], "tenantId"):
        raise ValueError("Inventory files have mismatched tenantId")
    for key in ("subscriptions", "excludedSubscriptions"):
        def coverage(doc):
            return sorted(
                (guid(item["id"], key), item["name"], item["state"], item.get("reason"))
                for item in doc[key]
            )
        if coverage(dns) != coverage(networks):
            raise ValueError(f"Inventory files have mismatched {key}")
    for zone in dns["privateDnsZones"]:
        validate_resource(zone, ZONE, dns_subs, "zone")
        for record in array(zone.get("aRecordSets"), "zone.aRecordSets"):
            identity(record, re.escape(zone["id"].rstrip("/")) + r"/A/.+", "A record")
            text(record.get("fqdn"), "A record.fqdn")
            ttl = record.get("ttl")
            if type(ttl) is not int or ttl < 0:
                raise ValueError("A record.ttl must be a nonnegative integer")
            nullable_fields(record, ("isAutoRegistered",), "A record", is_boolean=True)
            for address in array(record.get("ipv4Addresses"), "A record.ipv4Addresses"):
                ipaddress.IPv4Address(text(address, "IPv4 address"))
        for link in array(zone.get("virtualNetworkLinks"), "zone.virtualNetworkLinks"):
            identity(link, re.escape(zone["id"].rstrip("/")) + r"/virtualNetworkLinks/[^/]+", "VNet link")
            boolean(link.get("registrationEnabled"), "VNet link.registrationEnabled")
            nullable_fields(link, ("virtualNetworkLinkState", "provisioningState"), "VNet link")
            identity(link.get("virtualNetwork"), VNET, "VNet link.virtualNetwork")
    for vnet in networks["virtualNetworks"]:
        validate_resource(vnet, VNET, net_subs, "VNet")
        text(vnet.get("location"), "VNet.location")
        for peering in array(vnet.get("peerings"), "VNet.peerings"):
            identity(peering, re.escape(vnet["id"].rstrip("/")) + r"/virtualNetworkPeerings/[^/]+", "peering")
            nullable_fields(peering, ("peeringState", "provisioningState"), "peering")
            nullable_fields(peering, (
                "allowVirtualNetworkAccess", "allowForwardedTraffic",
                "allowGatewayTransit", "useRemoteGateways",
            ), "peering", is_boolean=True)
            identity(peering.get("remoteVirtualNetwork"), VNET, "peering.remoteVirtualNetwork")


def create_rows(dns, networks, hub_id):
    validate_documents(dns, networks)
    identity({"name": "hub", "id": hub_id}, VNET, "--hub-vnet-id")
    hub_key = normalized_id(hub_id)
    hubs = [v for v in networks["virtualNetworks"] if normalized_id(v["id"]) == hub_key]
    if not hubs:
        raise ValueError("Hub VNet was not found in the VNet inventory")
    hub = hubs[0]
    eligible = {hub_key}
    for vnet in networks["virtualNetworks"]:
        local_id = normalized_id(vnet["id"])
        for peering in vnet["peerings"]:
            remote_id = normalized_id(peering["remoteVirtualNetwork"]["id"])
            if local_id == hub_key:
                eligible.add(remote_id)
            if remote_id == hub_key:
                eligible.add(local_id)
    rows = {}
    for zone in dns["privateDnsZones"]:
        for link in zone["virtualNetworkLinks"]:
            linked = link["virtualNetwork"]
            linked_id = normalized_id(linked["id"])
            if linked_id not in eligible:
                continue
            key = (normalized_id(zone["id"]), normalized_id(link["id"]))
            row = dict(zip(COLUMNS, (
                zone["name"], zone["id"], zone["subscriptionId"], zone["resourceGroup"],
                link["name"], link["id"], linked["name"], linked["id"],
                "hub" if linked_id == hub_key else "directPeer", hub["name"], hub["id"],
                dns["tenantId"],
            )))
            if key in rows and rows[key] != row:
                existing = rows[key]
                id_columns = (
                    "privateDnsZoneId", "privateDnsZoneSubscriptionId", "virtualNetworkLinkId",
                    "linkedVnetId", "hubVnetId",
                )
                if any(
                    normalized_id(existing[c]) != normalized_id(row[c]) if c in id_columns
                    else existing[c] != row[c] for c in COLUMNS
                ):
                    raise ValueError(f"Conflicting duplicate link evidence: {link['id']}")
                continue
            rows[key] = row
    return [rows[key] for key in sorted(rows)]


def load_json(path):
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=reject_duplicate_keys)


def write_csv(rows, destination):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--inventory-json", type=Path, help="Combined tenant inventory")
    inputs.add_argument("--private-dns-json", type=Path, help="Legacy DNS inventory (requires --vnets-json)")
    parser.add_argument("--vnets-json", type=Path, help="Legacy VNet inventory")
    parser.add_argument("--hub-vnet-id", required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.inventory_json is not None and args.vnets_json is not None:
        parser.error("--vnets-json cannot be used with --inventory-json")
    if args.private_dns_json is not None and args.vnets_json is None:
        parser.error("--vnets-json is required with --private-dns-json")
    input_paths = (
        (args.inventory_json,) if args.inventory_json is not None
        else (args.private_dns_json, args.vnets_json)
    )
    try:
        if args.output_csv.resolve() in tuple(path.resolve() for path in input_paths):
            raise ValueError("--output-csv must not overwrite an inventory input")
        if args.inventory_json is not None:
            dns = networks = load_json(args.inventory_json)
        else:
            dns = load_json(args.private_dns_json)
            networks = load_json(args.vnets_json)
        rows = create_rows(dns, networks, args.hub_vnet_id)
        write_csv(rows, args.output_csv)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {len(rows)} matching zone/link rows to {args.output_csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

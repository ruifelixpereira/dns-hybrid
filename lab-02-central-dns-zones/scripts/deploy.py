#!/usr/bin/env python3
"""Deploy CSV-selected private DNS zones and keep only their resolver VNet links."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
COLUMNS = (
    "privateDnsZoneName", "tenantId", "privateDnsZoneId",
    "privateDnsZoneSubscriptionId", "privateDnsZoneResourceGroup",
    "centralTenantOperation", "originalTenantOperation",
)
MAX_BATCH_SIZE = 800


def guid(value, context):
    try:
        result = str(UUID(value))
    except ValueError as exc:
        raise ValueError(f"{context}: expected a GUID") from exc
    if result != value.casefold():
        raise ValueError(f"{context}: expected a GUID")
    return result


def zone_name(value, context):
    name = value.rstrip(".").casefold()
    if (
        not name or len(name) > 253
        or any(character.isspace() or ord(character) < 32 or character in "/\\" for character in name)
        or any(not label or len(label) > 63 for label in name.split("."))
    ):
        raise ValueError(f"{context}: invalid private DNS zone name")
    return name


def load_zone_names(path, central_tenant):
    create = set()
    existing = set()
    selected = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not set(COLUMNS).issubset(headers):
            raise ValueError(f"{path}: expected unique headers including {','.join(COLUMNS)}")
        for row in reader:
            context = f"{path}:{reader.line_num}"
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{context}: row length does not match headers")
            if not all(row[column].strip() for column in COLUMNS):
                raise ValueError(f"{context}: missing required plan metadata")
            source_tenant = guid(row["tenantId"], f"{context}.tenantId")
            guid(row["privateDnsZoneSubscriptionId"], f"{context}.privateDnsZoneSubscriptionId")
            operation = row["centralTenantOperation"]
            if operation not in ("create", "repeated", "none"):
                raise ValueError(f"{context}: unsupported centralTenantOperation: {operation}")
            if (operation == "none") != (source_tenant == central_tenant):
                raise ValueError(f"{context}: plan operations disagree with the specified central tenant")
            expected_original = "none" if operation == "none" else "delete"
            if row["originalTenantOperation"] != expected_original:
                raise ValueError(f"{context}: inconsistent originalTenantOperation")
            name = zone_name(row["privateDnsZoneName"], context)
            selected.add(name)
            if operation == "create":
                create.add(name)
            elif operation == "none":
                existing.add(name)
    conflicts = create & existing
    if conflicts:
        raise ValueError(f"{path}: domains marked both create and none: {', '.join(sorted(conflicts))}")
    return sorted(create), selected


def vnet_id(value):
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft.Network/virtualNetworks/([^/]+)",
        value, re.IGNORECASE,
    )
    if not match or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("--resolver-vnet-id: expected a virtual network resource ID")
    return guid(match[1], "--resolver-vnet-id subscription")


def azure_query(*arguments, output="tsv"):
    result = subprocess.run(
        ["az", *arguments, "--output", output, "--only-show-errors"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def plan_vnet_links(subscription, group, existing, planned, resolver):
    name = "resolver-" + hashlib.sha256(resolver.casefold().encode()).hexdigest()[:24]
    missing = []
    removals = []
    for zone in sorted(existing | set(planned)):
        links = []
        if zone in existing:
            links = json.loads(azure_query(
                "network", "private-dns", "link", "vnet", "list",
                "--subscription", subscription, "--resource-group", group,
                "--zone-name", zone, output="json",
            ))
            if not isinstance(links, list) or any(
                not isinstance(link, dict) or not isinstance(link.get("name"), str) or not link["name"]
                or not isinstance(link.get("virtualNetwork"), dict)
                or not isinstance(link["virtualNetwork"].get("id"), str)
                or not link["virtualNetwork"]["id"]
                for link in links
            ):
                raise ValueError(f"{zone}: invalid Azure VNet link response")
        removals.extend(
            {"zoneName": zone, "name": link["name"], "vnetId": link["virtualNetwork"]["id"]}
            for link in links if link["virtualNetwork"]["id"].casefold() != resolver.casefold()
        )
        matches = [link for link in links if link["virtualNetwork"]["id"].casefold() == resolver.casefold()]
        if matches:
            if any(link.get("provisioningState") != "Succeeded" for link in matches):
                raise ValueError(f"{zone}: existing resolver VNet link is not successfully provisioned")
            continue
        used_names = {link["name"].casefold() for link in links}
        link_name = name
        suffix = 1
        while link_name in used_names:
            link_name = f"{name}-{suffix}"
            suffix += 1
        missing.append({"zoneName": zone, "name": link_name})
    return missing, removals


def deployment_batches(names, links):
    pending = {link["zoneName"]: link for link in links}
    # New zones and their links stay together, within the ARM resource limit.
    for offset in range(0, len(names), MAX_BATCH_SIZE // 2):
        zones = names[offset:offset + MAX_BATCH_SIZE // 2]
        yield zones, [pending.pop(zone) for zone in zones if zone in pending]
    remaining = list(pending.values())
    for offset in range(0, len(remaining), MAX_BATCH_SIZE):
        yield [], remaining[offset:offset + MAX_BATCH_SIZE]


def preview_link_removals(group_id, removals):
    print("\nPlanned VNet link deletions (Azure CLI cleanup, not ARM what-if):")
    print(f"Resources: {len(removals)} to delete.")
    for link in removals:
        resource_id = (
            f"{group_id}/providers/Microsoft.Network/privateDnsZones/"
            f"{link['zoneName']}/virtualNetworkLinks/{link['name']}"
        )
        print(f"  - Delete {resource_id}")
        print(f"      virtualNetwork.id: {link['vnetId']}")
    print("Preview only. Links are deleted only by a confirmed create after successful deployments.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "what-if", "create"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--resolver-vnet-id", required=True)
    parser.add_argument("--input-csv", type=Path, default=Path("./inventory/hub-dns-zones.csv"))
    args = parser.parse_args(argv)
    try:
        tenant = guid(args.tenant_id, "--tenant-id")
        subscription = guid(args.subscription_id, "--subscription-id")
        resolver_subscription = vnet_id(args.resolver_vnet_id)
        if not args.resource_group.strip() or args.resource_group.startswith("-"):
            raise ValueError("--resource-group must be a nonempty resource group name")
        names, selected = load_zone_names(args.input_csv, tenant)
        actual_tenant = azure_query("account", "show", "--subscription", subscription, "--query", "tenantId")
        if guid(actual_tenant, "Azure subscription tenantId") != tenant:
            raise ValueError("Tenant mismatch for the specified subscription. Nothing deployed.")
        group_id = azure_query(
            "group", "show", "--subscription", subscription,
            "--name", args.resource_group, "--query", "id",
        )
        if not group_id:
            raise ValueError("Destination resource group lookup returned no ID. Nothing deployed.")
        if resolver_subscription != subscription:
            resolver_tenant = azure_query(
                "account", "show", "--subscription", resolver_subscription, "--query", "tenantId",
            )
            if guid(resolver_tenant, "Resolver subscription tenantId") != tenant:
                raise ValueError("Resolver VNet must belong to the specified central tenant. Nothing deployed.")
        actual_vnet = azure_query(
            "network", "vnet", "show", "--ids", args.resolver_vnet_id, "--query", "id",
        )
        if actual_vnet.casefold() != args.resolver_vnet_id.casefold():
            raise ValueError("Resolver VNet lookup returned an unexpected ID. Nothing deployed.")
        existing = {
            zone_name(name, "Azure destination zone") for name in azure_query(
                "network", "private-dns", "zone", "list",
                "--subscription", subscription, "--resource-group", args.resource_group,
                "--query", "[].name",
            ).splitlines()
        } & selected
        links, removals = plan_vnet_links(
            subscription, args.resource_group, existing, names, args.resolver_vnet_id,
        )
        if not names and not links and not removals:
            print("No create operations, missing resolver VNet links, or other VNet links; "
                  "no changes performed.", file=sys.stderr)
            if args.action == "what-if":
                preview_link_removals(group_id, removals)
            return 0
        scope = f"{tenant}/{subscription}/{args.resource_group}"
        print(f"{args.action}: {len(names)} private DNS zones, {len(links)} missing resolver VNet links, "
              f"and {len(removals)} other VNet links to remove "
              f"in {scope}; resolver VNet: {args.resolver_vnet_id}", file=sys.stderr)
        for link in removals:
            print(f"Remove VNet link: {link['zoneName']}/{link['name']} -> {link['vnetId']}",
                  file=sys.stderr)
        if args.action == "create":
            confirmation = input(
                f"Type '{scope}' to deploy the selected private DNS zones and resolver VNet links "
                f"and remove {len(removals)} other VNet links: "
            )
            if confirmation != scope:
                raise ValueError("Deployment cancelled: confirmation did not match the destination.")
        with tempfile.TemporaryDirectory(prefix="dns-central-zones-") as directory:
            parameters_path = Path(directory) / "parameters.json"
            for index, (zones, batch_links) in enumerate(deployment_batches(names, links), 1):
                parameters = {
                    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                    "contentVersion": "1.0.0.0",
                    "parameters": {
                        "zoneNames": {"value": zones},
                        "resolverVnetId": {"value": args.resolver_vnet_id},
                        "vnetLinks": {"value": batch_links},
                    },
                }
                parameters_path.write_text(json.dumps(parameters), encoding="utf-8")
                subprocess.run([
                    "az", "deployment", "group", args.action,
                    "--subscription", subscription, "--resource-group", args.resource_group,
                    "--name", f"dns-central-zones-{index:03d}",
                    "--mode", "Incremental", "--template-file", str(ROOT / "main.bicep"),
                    "--parameters", f"@{parameters_path}", "--only-show-errors",
                ], check=True)
        if args.action == "what-if":
            preview_link_removals(group_id, removals)
        if args.action == "create":
            # Keep existing connectivity until every resolver link deployment succeeds.
            for link in removals:
                azure_query(
                    "network", "private-dns", "link", "vnet", "delete",
                    "--subscription", subscription, "--resource-group", args.resource_group,
                    "--zone-name", link["zoneName"], "--name", link["name"], "--yes",
                )
                print(f"Removed VNet link: {link['zoneName']}/{link['name']}", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or str(exc)
        print(f"Error: Azure CLI command failed (exit {exc.returncode}): {detail}", file=sys.stderr)
        return 1
    except EOFError:
        print("Error: Deployment cancelled: confirmation input ended.", file=sys.stderr)
        return 1
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

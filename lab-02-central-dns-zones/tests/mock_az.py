#!/usr/bin/env python3
"""Strict Azure CLI double for offline central-zone deployment tests."""

import json
import os
from pathlib import Path
import sys


args = sys.argv[1:]
entry = {"args": args}
if len(args) > 2 and args[:2] == ["deployment", "group"]:
    try:
        parameter_file = args[args.index("--parameters") + 1]
        if not parameter_file.startswith("@"):
            raise ValueError("Expected a JSON parameter file")
        entry["parameters"] = json.loads(Path(parameter_file[1:]).read_text())
    except (ValueError, IndexError, OSError) as exc:
        print(f"Mock contract violation: {exc}", file=sys.stderr)
        sys.exit(10)
with Path(os.environ["MOCK_LOG"]).open("a") as stream:
    stream.write(json.dumps(entry) + "\n")

subscription = os.environ["MOCK_SUBSCRIPTION"]
group = os.environ["MOCK_RESOURCE_GROUP"]
mode = os.environ.get("MOCK_MODE", "")
vnet = f"/subscriptions/{subscription}/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver"


def fail(message, code=3):
    print(message, file=sys.stderr)
    sys.exit(code)


if args == [
    "account", "show", "--subscription", subscription,
    "--query", "tenantId", "--output", "tsv", "--only-show-errors",
]:
    print("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" if mode == "tenant-mismatch" else os.environ["MOCK_TENANT"])
elif args == [
    "group", "show", "--subscription", subscription, "--name", group,
    "--query", "id", "--output", "tsv", "--only-show-errors",
]:
    if mode == "missing-group":
        fail("ResourceGroupNotFound")
    print(f"/subscriptions/{subscription}/resourceGroups/{group}")
elif args == [
    "network", "vnet", "show", "--ids", vnet, "--query", "id",
    "--output", "tsv", "--only-show-errors",
]:
    if mode == "missing-vnet":
        fail("ResourceNotFound")
    print(vnet)
elif args == [
    "network", "private-dns", "zone", "list", "--subscription", subscription,
    "--resource-group", group, "--query", "[].name",
    "--output", "tsv", "--only-show-errors",
]:
    if mode == "zone-list-failure":
        fail("AuthorizationFailed: cannot list zones")
    print("existing.example\noutside-plan.example")
    if mode == "stale-plan":
        print("new.example")
elif args[:5] == ["network", "private-dns", "link", "vnet", "list"]:
    zone = args[args.index("--zone-name") + 1]
    expected = [
        "network", "private-dns", "link", "vnet", "list",
        "--subscription", subscription, "--resource-group", group,
        "--zone-name", zone, "--output", "json", "--only-show-errors",
    ]
    if args != expected or zone not in ("existing.example", "outside-plan.example", "new.example"):
        fail(f"Mock contract violation: {args}", 10)
    if mode == "link-list-failure":
        fail("AuthorizationFailed: cannot list links")
    if mode == "malformed-links":
        print("{}")
    else:
        links = [{
            "name": "arbitrary-existing-name", "virtualNetwork": {"id": vnet.upper()},
            "provisioningState": "Failed" if mode == "failed-link" else "Succeeded",
            "registrationEnabled": True,
        }] if zone == "existing.example" or mode in ("all-linked", "stale-plan") else []
        if mode == "missing-existing-link":
            links = [{"name": "other-vnet", "virtualNetwork": {"id": vnet + "-other"},
                      "provisioningState": "Succeeded"}]
        if mode in ("other-links", "cleanup-only", "delete-failure"):
            if mode == "cleanup-only" and not links:
                links = [{"name": "resolver-link", "virtualNetwork": {"id": vnet},
                          "provisioningState": "Succeeded"}]
            links.extend([
                {"name": "other-vnet", "virtualNetwork": {"id": vnet + "-other"},
                 "provisioningState": "Succeeded"},
                {"name": "another-vnet", "virtualNetwork": {"id": vnet + "-another"},
                 "provisioningState": "Succeeded"},
            ])
        print(json.dumps(links))
elif args[:5] == ["network", "private-dns", "link", "vnet", "delete"]:
    zone = args[args.index("--zone-name") + 1]
    name = args[args.index("--name") + 1]
    expected = [
        "network", "private-dns", "link", "vnet", "delete",
        "--subscription", subscription, "--resource-group", group,
        "--zone-name", zone, "--name", name, "--yes",
        "--output", "tsv", "--only-show-errors",
    ]
    if args != expected or zone not in ("existing.example", "outside-plan.example"):
        fail(f"Mock contract violation: {args}", 10)
    if name not in ("other-vnet", "another-vnet"):
        fail(f"Mock contract violation: refusing resolver link deletion: {name}", 10)
    if mode == "delete-failure":
        fail("AuthorizationFailed: cannot delete link")
elif len(args) > 3 and args[:2] == ["deployment", "group"] and args[2] in ("validate", "what-if", "create"):
    try:
        assert args[args.index("--subscription") + 1] == subscription
        assert args[args.index("--resource-group") + 1] == group
        assert args[args.index("--mode") + 1] == "Incremental"
        assert Path(args[args.index("--template-file") + 1]).name == "main.bicep"
        assert args[-1] == "--only-show-errors"
        names = entry["parameters"]["parameters"]["zoneNames"]["value"]
        links = entry["parameters"]["parameters"]["vnetLinks"]["value"]
        assert 1 <= len(names) + len(links) <= 800
        assert entry["parameters"]["parameters"]["resolverVnetId"]["value"] == vnet
        assert len({link["zoneName"] for link in links}) == len(links)
        assert all(set(link) == {"zoneName", "name"} for link in links)
    except (AssertionError, ValueError, KeyError, IndexError) as exc:
        fail(f"Mock contract violation: {exc}", 10)
    if mode == "deployment-failure":
        fail("AuthorizationFailed: deployment denied")
    print(json.dumps({"state": "Succeeded"}))
else:
    fail(f"Unexpected Azure CLI request: {args}", 10)

#!/usr/bin/env python3
"""Strict, offline Azure CLI double; never invokes the real az executable."""

import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, unquote, urlsplit


FIXTURES = Path(__file__).parent / "fixtures"
DNS = json.loads((FIXTURES / "private-dns-zones.json").read_text())
NETWORKS = json.loads((FIXTURES / "virtual-networks.json").read_text())
ENDPOINT = "https://management.azure.com"
args = sys.argv[1:]
with Path(os.environ["MOCK_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def resource(value, properties=None):
    result = {key: value[key] for key in ("name", "id")}
    if "location" in value:
        result["location"] = value["location"]
    if properties is not None:
        result["properties"] = {key: item for key, item in properties.items() if item is not None}
    return result


def raw_records(zone):
    return [
        resource(record, {
            "fqdn": record["fqdn"], "ttl": record["ttl"],
            "isAutoRegistered": record["isAutoRegistered"],
            "aRecords": [{"ipv4Address": ip} for ip in record["ipv4Addresses"]],
        }) for record in zone["aRecordSets"]
    ]


def raw_links(zone):
    return [
        resource(link, {
            key: value for key, value in link.items() if key not in ("name", "id")
        }) for link in zone["virtualNetworkLinks"]
    ]


def raw_peerings(vnet):
    return [
        resource(peer, {
            key: value for key, value in peer.items() if key not in ("name", "id")
        }) for peer in vnet["peerings"]
    ]


try:
    assert args[-3:] == ["--only-show-errors", "--output", "json"], args
    args = args[:-3]
    mode = os.environ.get("MOCK_MODE", "")
    if mode in ("hub-other-subscription", "hub-dns-link-tie", "hub-id-tie"):
        target = NETWORKS["virtualNetworks"][2 if mode == "hub-other-subscription" else 1]
        template = target["peerings"][0]
        count = 3 if mode == "hub-other-subscription" else 2
        target["peerings"] = [
            {**template, "name": f"extra-{index}",
             "id": target["id"] + f"/virtualNetworkPeerings/extra-{index}"}
            for index in range(count)
        ]
        if mode == "hub-dns-link-tie":
            zone = DNS["privateDnsZones"][1]
            link = DNS["privateDnsZones"][0]["virtualNetworkLinks"][1]
            zone["virtualNetworkLinks"].append({
                **link, "name": "extra-spoke-link",
                "id": zone["id"] + "/virtualNetworkLinks/extra-spoke-link",
                "virtualNetwork": {"name": target["name"], "id": target["id"].upper() + "/"},
            })
    if mode == "hub-zero-peerings":
        for vnet in NETWORKS["virtualNetworks"]:
            vnet["peerings"] = []
        for zone in DNS["privateDnsZones"]:
            zone["virtualNetworkLinks"] = []
    if args == ["account", "list", "--all", "--refresh"]:
        accounts = [
            {**item, "tenantId": DNS["tenantId"]}
            for item in DNS["subscriptions"] + DNS["excludedSubscriptions"]
        ]
        accounts.append({
            "name": "Foreign", "id": "55555555-5555-5555-5555-555555555555",
            "tenantId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "state": "Enabled",
        })
        if mode == "no-subscriptions":
            accounts = []
        print(json.dumps(accounts))
    elif args == ["cloud", "show"]:
        print(json.dumps({"endpoints": {"resourceManager": ENDPOINT + "/"}}))
    else:
        assert args[:4] == ["rest", "--method", "get", "--url"], args
        assert args[5] == "--subscription" and len(args) == 7, args
        url, subscription = args[4], args[6]
        assert subscription in {s["id"] for s in DNS["subscriptions"]}, args
        parsed = urlsplit(url)
        path = unquote(parsed.path)
        assert path.startswith(f"/subscriptions/{subscription}/"), args
        assert parsed.scheme == "https" and parsed.netloc == "management.azure.com", args
        query = parse_qs(parsed.query)
        dns_query = "/privateDnsZones" in path
        assert query["api-version"] == ["2018-09-01" if dns_query else "2026-03-01"], args
        if mode == "subscription-failure" and subscription.startswith("22222222"):
            fail("AuthorizationFailed: subscription denied", 3)
        if mode == "child-failure" and path.endswith("/virtualNetworkLinks"):
            fail("AuthorizationFailed: child query denied", 3)
        if mode in ("transient-once", "transient-always") and path.endswith("/privateDnsZones"):
            state = Path(os.environ["MOCK_LOG"] + ".retry")
            if not state.exists() or mode == "transient-always":
                state.touch()
                fail("TooManyRequests: 429", 3)
        collections = {
            f"/subscriptions/{subscription}/providers/Microsoft.Network/privateDnsZones": [
                resource(zone) for zone in DNS["privateDnsZones"] if zone["subscriptionId"] == subscription
            ],
            f"/subscriptions/{subscription}/providers/Microsoft.Network/virtualNetworks": [
                resource(vnet) for vnet in NETWORKS["virtualNetworks"] if vnet["subscriptionId"] == subscription
            ],
        }
        for zone in DNS["privateDnsZones"]:
            collections[zone["id"] + "/A"] = raw_records(zone)
            collections[zone["id"] + "/virtualNetworkLinks"] = raw_links(zone)
        for vnet in NETWORKS["virtualNetworks"]:
            collections[vnet["id"] + "/virtualNetworkPeerings"] = raw_peerings(vnet)
        assert path in collections, f"Unexpected resource request: {path}"
        values = [] if mode == "empty" else collections[path]
        page = int(query.get("page", ["0"])[0])
        response = {"value": values[page:page + 1]}
        if page + 1 < len(values):
            base_url = url.split("&page=")[0]
            response["nextLink"] = f"{base_url}&page={page + 1}"
        if mode == "cycle":
            response["nextLink"] = url
        if mode == "foreign-next-link":
            response["nextLink"] = "https://example.invalid/leak"
        if mode == "malformed":
            response = {"value": {}}
        if mode == "missing-child-id" and path.endswith("/A") and response["value"]:
            response["value"][0].pop("id")
        print(json.dumps(response))
except (AssertionError, KeyError, ValueError) as exc:
    fail(f"Mock contract violation: {exc}", 10)

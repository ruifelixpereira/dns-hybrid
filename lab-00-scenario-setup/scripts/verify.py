#!/usr/bin/env python3
"""Verify deployed lab configuration; optionally execute DNS checks on its VM."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

NETWORK_API = "2025-01-01"
DNS_API = "2025-05-01"
PRIVATE_DNS_API = "2024-06-01"
COMPUTE_API = "2024-11-01"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def ids(values):
    return {value.lower() for value in values}


def assert_run_succeeded(command):
    view = command["properties"].get("instanceView", {})
    require(
        view.get("executionState") == "Succeeded" and view.get("exitCode") == 0,
        f"Run Command did not succeed: {json.dumps(view)}",
    )
    print(view.get("output", ""))


class Azure:
    def __init__(self, subscription):
        self.subscription = subscription

    def cli(self, *args):
        result = subprocess.run(
            ["az", *args, "--subscription", self.subscription, "--output", "json"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return json.loads(result.stdout)

    def get(self, resource_id, version, expand=None):
        url = f"https://management.azure.com{resource_id}?api-version={version}"
        if expand:
            url += f"&$expand={expand}"
        return self.cli("rest", "--method", "get", "--url", url)

    def list(self, resource_id, version):
        page = self.get(resource_id, version)
        values = list(page["value"])
        while page.get("nextLink"):
            page = self.cli("rest", "--method", "get", "--url", page["nextLink"])
            values.extend(page["value"])
        return values


def verify(azure, lab):
    vnets = lab["vnetIds"]
    expected_roles = {"hub", "spoke1", "spoke2", "test", "endpoints", "resolver", "standalone", "onprem"}
    require(set(vnets) == expected_roles, "Unexpected VNet topology in deployment outputs.")
    participants = ids(vnets[role] for role in expected_roles - {"standalone", "onprem"})
    require(ids(lab["participatingVnetIds"]) == participants, "Incorrect participating VNets.")

    hub_peers = {"spoke1", "spoke2", "test", "endpoints", "resolver", "onprem"}
    for role, resource_id in vnets.items():
        vnet = azure.get(resource_id, NETWORK_API)
        properties = vnet["properties"]
        require(not properties.get("dhcpOptions", {}).get("dnsServers"), f"{role} uses custom VNet DNS.")
        expected = hub_peers if role == "hub" else set()
        if role in hub_peers:
            expected = {"hub"}
        if role in {"onprem", "resolver"}:
            expected = expected | ({"resolver"} if role == "onprem" else {"onprem"})
        peerings = azure.list(f"{resource_id}/virtualNetworkPeerings", NETWORK_API)
        require(
            ids(p["properties"]["remoteVirtualNetwork"]["id"] for p in peerings) == ids(vnets[p] for p in expected),
            f"{role} has unexpected peerings.",
        )
        for peering in peerings:
            config = peering["properties"]
            require(config["peeringState"] == "Connected", f"Peering not connected: {peering['id']}")
            require(config["allowVirtualNetworkAccess"], f"Peering access disabled: {peering['id']}")
            require(not any(config.get(flag, False) for flag in (
                "allowForwardedTraffic", "allowGatewayTransit", "useRemoteGateways"
            )), f"Unexpected transit configuration: {peering['id']}")

    for zone_id in lab["zoneIds"]:
        links = azure.list(f"{zone_id}/virtualNetworkLinks", PRIVATE_DNS_API)
        require(ids(link["properties"]["virtualNetwork"]["id"] for link in links) == participants,
                f"Incorrect private-zone links: {zone_id}")
        require(all(not link["properties"]["registrationEnabled"] for link in links),
                f"Auto-registration is enabled: {zone_id}")

    resolver = azure.get(lab["resolverId"], DNS_API)["properties"]
    require(resolver["virtualNetwork"]["id"].lower() == vnets["resolver"].lower(), "Resolver in wrong VNet.")
    ruleset = azure.get(lab["rulesetId"], DNS_API)["properties"]
    require(ids(item["id"] for item in ruleset["dnsResolverOutboundEndpoints"]) ==
            ids([lab["resolverId"] + "/outboundEndpoints/outbound"]), "Incorrect outbound endpoint association.")
    links = azure.list(f"{lab['rulesetId']}/virtualNetworkLinks", DNS_API)
    require(ids(link["properties"]["virtualNetwork"]["id"] for link in links) == participants,
            "Incorrect forwarding-ruleset links.")
    rules = azure.list(f"{lab['rulesetId']}/forwardingRules", DNS_API)
    require(len(rules) == 1, "Expected only the demo-lab rule; avoid wildcard/inbound forwarding loops.")
    rule = rules[0]["properties"]
    require(rule["domainName"] == "demo-lab." and rule["forwardingRuleState"] == "Enabled",
            "Incorrect demo-lab forwarding rule.")
    require(rule["targetDnsServers"] == [{"ipAddress": lab["dnsVm"]["privateIp"], "port": 53}],
            "Incorrect Windows DNS forwarding target.")
    inbound = azure.get(lab["resolverId"] + "/inboundEndpoints/inbound", DNS_API)["properties"]
    require(inbound["ipConfigurations"][0]["privateIpAddress"] == lab["inboundIp"], "Incorrect inbound IP.")

    services = lab["services"]
    for kind, group, fqdn in (("blob", "blob", "blobFqdn"), ("vault", "vault", "vaultFqdn")):
        endpoint = azure.get(services[f"{kind}EndpointId"], NETWORK_API)["properties"]
        require(endpoint["subnet"]["id"].lower() == (vnets["endpoints"] + "/subnets/workload").lower(),
                f"{kind} endpoint in wrong subnet.")
        connections = endpoint["privateLinkServiceConnections"]
        require(len(connections) == 1 and connections[0]["properties"]["groupIds"] == [group],
                f"{kind} endpoint has incorrect subresource.")
        require(connections[0]["properties"]["privateLinkServiceConnectionState"]["status"] == "Approved",
                f"{kind} endpoint is not approved.")
        expected_service = services["storageId" if kind == "blob" else "vaultId"]
        require(connections[0]["properties"]["privateLinkServiceId"].lower() == expected_service.lower(),
                f"{kind} endpoint targets the wrong service.")
        nic = azure.get(endpoint["networkInterfaces"][0]["id"], NETWORK_API)["properties"]
        actual_ips = {config["properties"]["privateIPAddress"] for config in nic["ipConfigurations"]}
        require(actual_ips == {services[f"{kind}Ip"]}, f"{kind} endpoint NIC IP differs from deployment outputs.")
        zone_groups = azure.list(services[f"{kind}EndpointId"] + "/privateDnsZoneGroups", NETWORK_API)
        zone_index = 0 if kind == "blob" else 1
        require(len(zone_groups) == 1, f"Expected one DNS zone group for {kind}.")
        configs = zone_groups[0]["properties"]["privateDnsZoneConfigs"]
        require(len(configs) == 1 and
                configs[0]["properties"]["privateDnsZoneId"].lower() == lab["zoneIds"][zone_index].lower(),
                f"Incorrect DNS zone group for {kind}.")
        print(f"{services[fqdn]} -> {services[f'{kind}Ip']}")

    for key, version in (("storageId", "2025-01-01"), ("vaultId", "2024-11-01")):
        service = azure.get(services[key], version)["properties"]
        require(service["publicNetworkAccess"] == "Disabled", f"Public access enabled: {key}")
    assert_run_succeeded(azure.get(lab["dnsVm"]["configurationId"], COMPUTE_API, "instanceView"))
    print("PASS deployed topology, private-zone links, ruleset, endpoints, and DNS bootstrap.")


def run_dns_tests(azure, lab):
    script = Path(__file__).with_name("Test-Dns.ps1").read_text()
    vm = lab["dnsVm"]
    azure.cli(
        "vm", "run-command", "create", "--resource-group", vm["resourceGroup"],
        "--vm-name", vm["name"], "--name", "validate-dns", "--script", script,
        "--async-execution", "false", "--timeout-in-seconds", "600",
        "--parameters",
        f"InboundIp={lab['inboundIp']}", f"DnsVmIp={vm['privateIp']}",
        f"BlobFqdn={lab['services']['blobFqdn']}", f"BlobIp={lab['services']['blobIp']}",
        f"VaultFqdn={lab['services']['vaultFqdn']}", f"VaultIp={lab['services']['vaultIp']}",
    )
    assert_run_succeeded(azure.get(vm["id"] + "/runCommands/validate-dns", COMPUTE_API, "instanceView"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True, help="Subscription GUID")
    parser.add_argument("--tenant", required=True, help="Expected tenant GUID")
    parser.add_argument("--deployment", required=True, help="Subscription deployment name")
    parser.add_argument("--dns-tests", action="store_true", help="Execute DNS tests using managed VM Run Command")
    args = parser.parse_args()
    azure = Azure(args.subscription)
    account = azure.cli("account", "show")
    require(account["tenantId"].lower() == args.tenant.lower(), "Account tenant mismatch.")
    deployment = azure.cli("deployment", "sub", "show", "--name", args.deployment)
    require(deployment["properties"]["provisioningState"] == "Succeeded", "Deployment did not succeed.")
    lab = deployment["properties"]["outputs"]["lab"]["value"]
    require(lab["tenantId"].lower() == args.tenant.lower() and
            lab["subscriptionId"].lower() == args.subscription.lower(), "Deployment scope mismatch.")
    verify(azure, lab)
    if args.dns_tests:
        run_dns_tests(azure, lab)
    else:
        print("Live DNS tests NOT run. Add --dns-tests to execute them.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Export central DNS forwarding rules or reconcile them on non-central resolvers."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys

import lab


API = "2022-07-01"


def domain_name(value):
    name = lab.text(value, "forwarding domain").strip().lower()
    if name == ".":
        return name
    name = name.removesuffix(".")
    labels = name.split(".")
    if (len(name) > 253 or not 1 <= len(labels) <= 34
            or any(not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label)
                   for label in labels)):
        raise ValueError(f"Invalid on-premises forwarding domain: {value!r}")
    return name + "."


def servers(values):
    result = []
    seen = set()
    for value in lab.collection(values, "targetDnsServers"):
        address = ipaddress.IPv4Address(lab.field(value, "ipAddress", "DNS server"))
        port = lab.object_value(value, "DNS server").get("port", 53)
        if (address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local
                or str(address) in ("168.63.129.16", "255.255.255.255")
                or type(port) is not int or not 1 <= port <= 65535):
            raise ValueError(f"Invalid forwarding DNS server: {address}:{port}")
        key = (str(address), port)
        if key in seen:
            raise ValueError(f"Duplicate forwarding DNS server: {address}:{port}")
        seen.add(key)
        result.append({"ipAddress": str(address), "port": port})
    if not 1 <= len(result) <= 6:
        raise ValueError("Each forwarding domain requires between 1 and 6 DNS servers")
    return result


def validate_rules(document):
    document = lab.object_value(document, "forwarding rules file")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
        raise ValueError("Forwarding rules file: expected schemaVersion 1")
    rules = {}
    for value in lab.collection(document.get("rules"), "forwarding rules"):
        domain = domain_name(lab.field(value, "domainName", "forwarding rule"))
        if domain in rules:
            raise ValueError(f"Duplicate forwarding domain: {domain}")
        value = lab.object_value(value, "forwarding rule")
        state = value.get("forwardingRuleState", "Enabled")
        if state not in ("Enabled", "Disabled"):
            raise ValueError(f"Invalid forwarding rule state for {domain}: {state!r}")
        rules[domain] = {"servers": servers(value.get("targetDnsServers")), "state": state}
    if not 1 <= len(rules) <= 1000:
        raise ValueError("Provide between 1 and 1000 forwarding domains")
    return rules


def load_rules(path):
    return validate_rules(lab.read_json(path))


def parts(resource_id, kind, subscription):
    match = lab.inventory.identity(
        {"name": "forwarding resource", "id": resource_id},
        lab.inventory.ARM_PREFIX + kind, "forwarding resource",
    )
    if lab.guid(match["subscription"], "forwarding subscription") != subscription:
        raise ValueError(f"Forwarding resource is outside the mapped subscription: {resource_id}")
    return match


def properties(resource, expected_id=None):
    resource_id = lab.field(resource, "id", "forwarding resource")
    if expected_id and lab.normalized(resource_id) != lab.normalized(expected_id):
        raise ValueError(f"Unexpected forwarding resource ID: {resource_id}")
    value = lab.object_value(resource.get("properties"), "forwarding properties")
    if value.get("provisioningState") != "Succeeded":
        raise ValueError(f"Forwarding resource is not healthy: {resource_id}")
    return value


def listing(resource_id, child, subscription):
    return lab.pages(f"{lab.ARM}{resource_id}/{child}?api-version={API}", subscription=subscription)


def get_resource(resource_id, subscription):
    return lab.az("rest", "--method", "get", "--url", f"{lab.ARM}{resource_id}?api-version={API}",
                  "--subscription", subscription)


def export_rules(central_tenant, source_id, output_file):
    match = lab.inventory.identity(
        {"name": "central ruleset", "id": source_id},
        lab.inventory.ARM_PREFIX + r"dnsForwardingRulesets/[^/]+", "central ruleset",
    )
    subscription = lab.guid(match["subscription"], "central ruleset subscription")
    lab.current_account(central_tenant, subscription, human=True)
    lab.live_subscription(subscription, central_tenant)
    properties(get_resource(source_id, subscription), source_id)
    exported = []
    for rule in listing(source_id, "forwardingRules", subscription):
        value = properties(rule)
        rule_id = lab.field(rule, "id", "central forwarding rule")
        lab.inventory.identity(rule, re.escape(source_id.rstrip("/")) + r"/forwardingRules/[^/]+",
                               "central forwarding rule")
        exported.append({
            "domainName": lab.field(value, "domainName", rule_id),
            "targetDnsServers": lab.collection(value.get("targetDnsServers"), "central rule DNS servers"),
            "forwardingRuleState": lab.field(value, "forwardingRuleState", rule_id),
        })
    rules = validate_rules({"schemaVersion": 1, "rules": exported})
    document = {
        "schemaVersion": 1, "sourceTenantId": central_tenant, "sourceRulesetId": source_id,
        "rules": [{"domainName": domain, "targetDnsServers": value["servers"],
                   "forwardingRuleState": value["state"]} for domain, value in sorted(rules.items())],
    }
    lab.save_state(output_file, document)
    print(f"Exported {len(rules)} central forwarding rules to {output_file}. "
          "No Azure resources changed.")


def choose_endpoint(target, options):
    subscription = target["subscriptionId"]
    resolvers = listing(lab.group_scope(subscription, target["resourceGroup"]),
                        "providers/Microsoft.Network/dnsResolvers", subscription)
    endpoints = []
    inbound_ips = set()
    for resolver in resolvers:
        value = lab.object_value(resolver.get("properties"), "resolver properties")
        if lab.normalized(lab.field(value.get("virtualNetwork"), "id", "resolver VNet")) != lab.normalized(target["resolverVnetId"]):
            continue
        properties(resolver)
        resolver_id = lab.field(resolver, "id", "resolver")
        match = parts(resolver_id, r"dnsResolvers/[^/]+", subscription)
        if match["group"].casefold() != target["resourceGroup"].casefold():
            raise ValueError(f"Resolver lookup returned an unexpected resource group: {resolver_id}")
        location = lab.field(resolver, "location", "resolver")
        for endpoint in listing(resolver_id, "outboundEndpoints", subscription):
            endpoint_id = lab.field(endpoint, "id", "outbound endpoint")
            parts(endpoint_id, r"dnsResolvers/[^/]+/outboundEndpoints/[^/]+", subscription)
            properties(endpoint)
            if not lab.normalized(endpoint_id).startswith(lab.normalized(resolver_id) + "/outboundendpoints/"):
                raise ValueError(f"Outbound endpoint is outside its resolver: {endpoint_id}")
            endpoints.append({"id": endpoint_id, "location": location, "resourceGroup": match["group"]})
        for inbound in listing(resolver_id, "inboundEndpoints", subscription):
            for config in lab.collection(properties(inbound).get("ipConfigurations"), "inbound IP configurations"):
                inbound_ips.add(lab.field(config, "privateIpAddress", "inbound IP"))
    selected_id = options.get("outboundEndpointId")
    if selected_id is not None:
        parts(lab.text(selected_id, "outboundEndpointId"), r"dnsResolvers/[^/]+/outboundEndpoints/[^/]+", subscription)
        endpoints = [endpoint for endpoint in endpoints if lab.normalized(endpoint["id"]) == lab.normalized(selected_id)]
    if len(endpoints) != 1:
        raise ValueError(f"Expected one outbound endpoint for {target['resolverVnetId']}; "
                         "set outboundEndpointId in its resolver-map entry when selection is ambiguous")
    return endpoints[0], inbound_ips


def plan_target(target, options, requested):
    subscription = target["subscriptionId"]
    endpoint, inbound_ips = choose_endpoint(target, options)
    if any(server["ipAddress"] in inbound_ips for value in requested.values()
           if value["state"] == "Enabled" for server in value["servers"]):
        raise ValueError("A forwarding target is this resolver VNet's inbound endpoint; linking it would create a DNS loop")
    vnet_id = target["resolverVnetId"]
    digest = hashlib.sha256(lab.normalized(vnet_id).encode()).hexdigest()[:20]
    new_id = (f"{lab.group_scope(subscription, endpoint['resourceGroup'])}"
              f"/providers/Microsoft.Network/dnsForwardingRulesets/dns-lab03-{digest}")
    resources = listing(lab.subscription_scope(subscription),
                        "providers/Microsoft.Network/dnsForwardingRulesets", subscription)
    candidates = []
    explicit_id = options.get("forwardingRulesetId")
    if explicit_id is not None:
        parts(lab.text(explicit_id, "forwardingRulesetId"), r"dnsForwardingRulesets/[^/]+", subscription)
    for resource in resources:
        resource_id = lab.field(resource, "id", "ruleset")
        parts(resource_id, r"dnsForwardingRulesets/[^/]+", subscription)
        value = lab.object_value(resource.get("properties"), "ruleset properties")
        outbound = [lab.normalized(lab.field(item, "id", "ruleset endpoint"))
                    for item in lab.collection(value.get("dnsResolverOutboundEndpoints"), "ruleset endpoints")]
        links = listing(resource_id, "virtualNetworkLinks", subscription)
        matching = [link for link in links if lab.normalized(lab.field(
            lab.object_value(link.get("properties"), "ruleset link properties").get("virtualNetwork"),
            "id", "ruleset link VNet")) == lab.normalized(vnet_id)]
        associated = lab.normalized(endpoint["id"]) in outbound
        if matching and not associated:
            raise ValueError(f"Resolver VNet already has a ruleset using a different outbound endpoint: {resource_id}")
        if (lab.normalized(resource_id) == lab.normalized(new_id)
                or explicit_id and lab.normalized(resource_id) == lab.normalized(explicit_id)) and not associated:
            raise ValueError(f"Ruleset endpoint conflict: {resource_id}")
        if associated:
            properties(resource)
            if lab.field(resource, "location", "ruleset").casefold() != endpoint["location"].casefold():
                raise ValueError(f"Ruleset and resolver regions differ: {resource_id}")
            if len(matching) > 1:
                raise ValueError(f"Duplicate ruleset links to resolver VNet: {resource_id}")
            for link in matching:
                properties(link)
            if not matching and len(links) >= 500:
                raise ValueError(f"Ruleset VNet link limit reached: {resource_id}")
            candidates.append((resource, links, bool(matching)))
    if explicit_id:
        candidates = [entry for entry in candidates if lab.normalized(entry[0]["id"]) == lab.normalized(explicit_id)]
        if not candidates:
            raise ValueError("forwardingRulesetId was not found with the selected outbound endpoint")
    else:
        linked = [entry for entry in candidates if entry[2]]
        if linked:
            candidates = linked
    if len(candidates) > 1:
        raise ValueError("Multiple eligible rulesets; specify forwardingRulesetId in the resolver-map entry")
    existing, links, linked = candidates[0] if candidates else (None, [], False)
    resource_id = existing["id"] if existing else new_id
    match = parts(resource_id, r"dnsForwardingRulesets/[^/]+", subscription)
    link_name = f"resolver-{digest}"
    if not linked and any(lab.field(link, "name", "ruleset link").casefold() == link_name for link in links):
        raise ValueError(f"Ruleset VNet link name collision: {resource_id}/{link_name}")
    current = listing(resource_id, "forwardingRules", subscription) if existing else []
    missing = []
    for domain, requested_rule in requested.items():
        targets = requested_rule["servers"]
        state = requested_rule["state"]
        matches = [rule for rule in current if domain_name(lab.field(
            lab.object_value(rule.get("properties"), "rule properties"), "domainName", "rule")) == domain]
        if len(matches) > 1:
            raise ValueError(f"Duplicate existing forwarding domain: {domain}")
        name = "domain-" + hashlib.sha256(domain.encode()).hexdigest()[:20]
        if matches:
            value = properties(matches[0])
            if value.get("forwardingRuleState") != state or servers(value.get("targetDnsServers")) != targets:
                raise ValueError(f"Conflicting forwarding rule for {domain}: {matches[0]['id']}")
        else:
            if any(lab.field(rule, "name", "rule").casefold() == name for rule in current):
                raise ValueError(f"Forwarding rule name collision: {name}")
            missing.append({"name": name, "domain": domain, "servers": targets, "state": state})
    for rule in current:
        value = lab.object_value(rule.get("properties"), "rule properties")
        if value.get("forwardingRuleState") == "Enabled" and any(
            server["ipAddress"] in inbound_ips for server in servers(value.get("targetDnsServers"))
        ):
            raise ValueError(f"Existing ruleset rule would loop through this resolver's inbound endpoint: {rule['id']}")
    if len(current) + len(missing) > 1000:
        raise ValueError(f"Ruleset forwarding rule limit reached: {resource_id}")
    actions = []
    if not existing:
        actions.append("Microsoft.Network/dnsForwardingRulesets/write")
    if missing:
        actions.append("Microsoft.Network/dnsForwardingRulesets/forwardingRules/write")
    if not linked:
        actions.append("Microsoft.Network/dnsForwardingRulesets/virtualNetworkLinks/write")
        lab.permissions(vnet_id, ["Microsoft.Network/virtualNetworks/read",
                                  "Microsoft.Network/virtualNetworks/join/action"], subscription=subscription)
    if actions:
        lab.permissions(resource_id if existing else lab.group_scope(subscription, match["group"]),
                        actions, subscription=subscription)
    return {
        "id": resource_id, "subscription": subscription, "group": match["group"],
        "name": resource_id.rstrip("/").rsplit("/", 1)[-1], "endpoint": endpoint,
        "vnet": vnet_id, "create": not existing, "link": not linked,
        "linkName": link_name, "rules": missing,
    }


def apply(plan):
    common = ["--subscription", plan["subscription"], "--resource-group", plan["group"]]
    if not plan["create"]:
        resource = get_resource(plan["id"], plan["subscription"])
        value = properties(resource, plan["id"])
        if (lab.normalized(plan["endpoint"]["id"]) not in [
            lab.normalized(lab.field(item, "id", "ruleset endpoint"))
            for item in lab.collection(value.get("dnsResolverOutboundEndpoints"), "ruleset endpoints")
        ] or lab.field(resource, "location", "ruleset").casefold() != plan["endpoint"]["location"].casefold()):
            raise ValueError(f"Reused ruleset endpoint/region changed after preview: {plan['id']}")
    if plan["create"]:
        lab.az("dns-resolver", "forwarding-ruleset", "create", *common, "--name", plan["name"],
               "--location", plan["endpoint"]["location"], "--outbound-endpoints",
               json.dumps([{"id": plan["endpoint"]["id"]}]), "--if-none-match", "*")
        resource = get_resource(plan["id"], plan["subscription"])
        value = properties(resource, plan["id"])
        if ([lab.normalized(lab.field(item, "id", "created ruleset endpoint")) for item in
             lab.collection(value.get("dnsResolverOutboundEndpoints"), "created ruleset endpoints")]
                != [lab.normalized(plan["endpoint"]["id"])]) or (
                lab.field(resource, "location", "created ruleset").casefold() != plan["endpoint"]["location"].casefold()):
            raise ValueError(f"Created ruleset does not match the planned endpoint/region: {plan['id']}")
        print(f"Created and verified ruleset: {plan['id']}", flush=True)
    for rule in plan["rules"]:
        lab.az("dns-resolver", "forwarding-rule", "create", *common, "--ruleset-name", plan["name"],
               "--name", rule["name"], "--domain-name", rule["domain"], "--forwarding-rule-state", rule["state"],
               "--target-dns-servers", json.dumps([
                   {"ip-address": server["ipAddress"], "port": server["port"]} for server in rule["servers"]
               ]), "--if-none-match", "*")
        rule_id = f"{plan['id']}/forwardingRules/{rule['name']}"
        value = properties(get_resource(rule_id, plan["subscription"]), rule_id)
        if (domain_name(value.get("domainName")) != rule["domain"]
                or value.get("forwardingRuleState") != rule["state"] or servers(value.get("targetDnsServers")) != rule["servers"]):
            raise ValueError(f"Created forwarding rule verification failed: {rule_id}")
        print(f"Created and verified rule: {rule_id}", flush=True)
    if plan["link"]:
        lab.az("dns-resolver", "vnet-link", "create", *common, "--ruleset-name", plan["name"],
               "--name", plan["linkName"], "--id", plan["vnet"], "--if-none-match", "*")
        link_id = f"{plan['id']}/virtualNetworkLinks/{plan['linkName']}"
        value = properties(get_resource(link_id, plan["subscription"]), link_id)
        if lab.normalized(lab.field(value.get("virtualNetwork"), "id", "created ruleset VNet")) != lab.normalized(plan["vnet"]):
            raise ValueError(f"Created ruleset VNet link verification failed: {link_id}")
        print(f"Created and verified ruleset VNet link: {link_id}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "validate", "what-if", "create"))
    parser.add_argument("--central-tenant-id", required=True, type=lambda value: lab.guid(value, "central tenant"))
    parser.add_argument("--tenant-id", type=lambda value: lab.guid(value, "non-central tenant"),
                        help="Limit setup to one non-central tenant; otherwise process all mapped tenants.")
    parser.add_argument("--inventory-directory", type=Path, default=Path("./.inventory"))
    parser.add_argument("--resolver-map", type=Path)
    parser.add_argument("--rules-file", type=Path, help="Defaults to INVENTORY_DIRECTORY/lab03-forwarding-rules.json.")
    parser.add_argument("--source-ruleset-id", help="Full central ruleset ARM ID; required for export.")
    parser.add_argument("--output-file", type=Path, help="Export output; defaults to INVENTORY_DIRECTORY/lab03-forwarding-rules.json.")
    args = parser.parse_args(argv)
    if args.action == "export" and not args.source_ruleset_id:
        parser.error("--source-ruleset-id is required for export")
    if args.action != "export" and not args.resolver_map:
        parser.error("--resolver-map is required for validation, preview, and creation")
    try:
        default_file = args.inventory_directory / "lab03-forwarding-rules.json"
        if args.action == "export":
            export_rules(args.central_tenant_id, args.source_ruleset_id, args.output_file or default_file)
            return 0
        rules = load_rules(args.rules_file or default_file)
        documents = lab.discover(args.inventory_directory, args.central_tenant_id)
        targets = lab.resolver_targets(args.resolver_map, documents)
        selected = [target for target in targets if args.tenant_id is None or target["tenantId"] == args.tenant_id]
        if not selected:
            raise ValueError("Forwarding destination must be a mapped non-central tenant")
        options = {lab.normalized(entry["resolverVnetId"]): entry
                   for entry in lab.read_json(args.resolver_map)["resolvers"]}
        tenants = sorted({target["tenantId"] for target in selected})
        for tenant, subscription in sorted({(target["tenantId"], target["subscriptionId"]) for target in selected}):
            lab.current_account(tenant, subscription, human=True)
        lab.check_resolvers(selected)
        plans = [plan_target(target, options[lab.normalized(target["resolverVnetId"])], rules) for target in selected]
        print("Script-generated forwarding preview (not ARM what-if); no deletions:")
        if any(not plan["create"] and plan["rules"] for plan in plans):
            print("WARNING: Adding rules to a reused ruleset affects every VNet already linked to it.")
        if "." in rules and rules["."]["state"] == "Enabled":
            print("WARNING: The root '.' rule forwards all matching namespaces; its targets must resolve public DNS.")
        print(f"  Non-central tenants: {', '.join(tenants)}")
        for plan in plans:
            print(f"  {'Create' if plan['create'] else 'Reuse'} ruleset: {plan['id']}")
            print(f"    Outbound endpoint: {plan['endpoint']['id']}")
            print(f"    {'Link' if plan['link'] else 'Keep link'} to VNet: {plan['vnet']}")
            for rule in plan["rules"]:
                print(f"    + {rule['domain']} ({rule['state']}) -> {json.dumps(rule['servers'])}")
        changes = [plan for plan in plans if plan["create"] or plan["link"] or plan["rules"]]
        if not changes:
            print("All requested rulesets, VNet links, and forwarding rules already match; no changes.")
        elif args.action == "create":
            lab.confirm("forwarding", ",".join(tenants))
            for plan in changes:
                apply(plan)
    except (OSError, ValueError, EOFError) as exc:
        print(f"Error: {exc or 'confirmation input ended'}", file=sys.stderr)
        print("Stopped. Earlier verified creations remain; no automatic rollback.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

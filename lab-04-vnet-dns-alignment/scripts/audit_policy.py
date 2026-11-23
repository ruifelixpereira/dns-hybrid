#!/usr/bin/env python3
"""Deploy Audit-only VNet DNS policies to inventory-covered subscriptions."""

import argparse
import json
from pathlib import Path
import sys

import align_dns as alignment


common = alignment.common
API = "2023-04-01"
NAME = "lab04-resolver-dns-audit"
DNS_ALIAS = "Microsoft.Network/virtualNetworks/dhcpOptions.dnsServers[*]"
MARKER = {"lab": "lab04-vnet-dns-alignment", "purpose": "resolver-dns-audit"}


def definition():
    return {"properties": {
        "displayName": "Lab 04 - Audit VNet resolver DNS configuration",
        "description": "Audit all VNets except the mapped resolver when their ordered DNS list differs from its inbound IPs.",
        "policyType": "Custom",
        "mode": "All",
        "metadata": {**MARKER, "category": "Network", "version": "1.0.0"},
        "parameters": {
            "resolverVnetId": {"type": "String", "metadata": {"displayName": "Excluded resolver VNet ID"}},
            "dnsServers": {"type": "Array", "metadata": {"displayName": "Expected ordered resolver inbound IPs"}},
        },
        "policyRule": {
            "if": {"allOf": [
                {"field": "type", "equals": "Microsoft.Network/virtualNetworks"},
                {"field": "id", "notEquals": "[parameters('resolverVnetId')]"},
                {"value": f"[equals(field('{DNS_ALIAS}'), parameters('dnsServers'))]", "equals": False},
            ]},
            "then": {"effect": "audit"},
        },
    }}


def assignment(definition_id, target, endpoint):
    return {"properties": {
        "displayName": "Lab 04 - Audit VNet resolver DNS configuration",
        "description": "Audit every VNet in this subscription except the mapped resolver VNet; never change DNS.",
        "metadata": {**MARKER, "tenantId": target["tenantId"]},
        "policyDefinitionId": definition_id,
        "enforcementMode": "DoNotEnforce",
        "parameters": {
            "resolverVnetId": {"value": target["resolverVnetId"]},
            "dnsServers": {"value": endpoint["dns"]},
        },
        "notScopes": [],
    }}


def existing(resource_id, subscription):
    collection = resource_id.rsplit("/", 1)[0]
    values = common.pages(f"{common.ARM}{collection}?api-version={API}", subscription=subscription)
    matches = [value for value in values
               if common.normalized(common.field(value, "id", "policy resource")) == common.normalized(resource_id)]
    if len(matches) > 1:
        raise ValueError(f"Duplicate policy resource IDs returned: {resource_id}")
    return matches[0] if matches else None


def matches(resource, desired):
    properties = common.object_value(resource.get("properties"), "policy properties")
    for key, value in desired["properties"].items():
        actual = properties.get(key)
        if key == "metadata":
            metadata = common.object_value(actual, "policy metadata")
            if any(metadata.get(name) != item for name, item in value.items()):
                return False
        elif key == "policyDefinitionId":
            if not isinstance(actual, str) or common.normalized(actual) != common.normalized(value):
                return False
        elif key == "notScopes":
            if actual not in (None, []):
                return False
        elif actual != value:
            return False
    return not properties.get("overrides") and not properties.get("resourceSelectors")


def plan_resource(resource_id, body, subscription, update_existing):
    previous = existing(resource_id, subscription)
    action = "Create"
    if previous is not None:
        properties = common.object_value(previous.get("properties"), "policy properties")
        metadata = common.object_value(properties.get("metadata"), "policy metadata")
        if any(metadata.get(key) != value for key, value in MARKER.items()):
            raise ValueError(f"Policy name is already used by a resource not owned by this lab: {resource_id}")
        if "tenantId" in body["properties"]["metadata"] and (
            metadata.get("tenantId") != body["properties"]["metadata"]["tenantId"]
        ):
            raise ValueError(f"Existing policy assignment belongs to another tenant: {resource_id}")
        if matches(previous, body):
            action = "Keep"
        else:
            is_assignment = "/policyAssignments/" in resource_id
            if not is_assignment or not update_existing:
                raise ValueError(f"Existing policy differs: {resource_id}. "
                                 "Only owned assignments can be refreshed with --update-existing; "
                                 "review conflicting definitions manually.")
            if (properties.get("notScopes") or properties.get("overrides") or properties.get("resourceSelectors")
                    or common.normalized(common.field(properties, "policyDefinitionId", "assignment"))
                    != common.normalized(body["properties"]["policyDefinitionId"])):
                raise ValueError(f"Existing assignment has different scope filters or definition: {resource_id}")
            action = "Update"
    if action != "Keep":
        resource_type = resource_id.rsplit("/", 2)[1]
        common.permissions(resource_id.rsplit("/providers/", 1)[0],
                           [f"Microsoft.Authorization/{resource_type}/write"], subscription=subscription)
    return {"id": resource_id, "body": body, "previous": previous, "subscriptionId": subscription, "action": action}


def check_alias(subscription):
    provider = common.object_value(
        common.az("provider", "show", "--namespace", "Microsoft.Network",
                  "--expand", "resourceTypes/aliases", "--subscription", subscription),
        "network provider",
    )
    aliases = {
        common.field(alias, "name", "policy alias")
        for resource in common.collection(provider.get("resourceTypes"), "provider resource types")
        if resource.get("resourceType") == "virtualNetworks"
        for alias in common.collection(resource.get("aliases"), "VNet policy aliases")
    }
    if DNS_ALIAS not in aliases:
        raise ValueError(f"Required Azure Policy DNS array alias is unavailable in {subscription}: {DNS_ALIAS}")


def apply(plans, endpoints):
    for target, endpoint in endpoints:
        live = common.az("rest", "--method", "get", "--url",
                         f"{common.ARM}{endpoint['id']}?api-version={alignment.DNS_API}",
                         "--subscription", target["subscriptionId"])
        if (common.normalized(common.field(live, "id", "inbound endpoint")) != common.normalized(endpoint["id"])
                or alignment.endpoint_ips(live, target["resolverVnetId"]) != endpoint["dns"]):
            raise ValueError(f"Inbound endpoint changed after preview: {endpoint['id']}; rerun what-if")
    for plan in plans:
        resource_id = plan["id"]
        subscription = plan["subscriptionId"]
        if plan["action"] != "Keep":
            if existing(resource_id, subscription) != plan["previous"]:
                raise ValueError(f"Policy changed after preview: {resource_id}; rerun what-if")
            print(f"{plan['action']} policy resource: {resource_id}", flush=True)
            common.az("rest", "--method", "put", "--url", f"{common.ARM}{resource_id}?api-version={API}",
                      "--body", json.dumps(plan["body"]), "--subscription", subscription)
        result = common.az("rest", "--method", "get", "--url",
                           f"{common.ARM}{resource_id}?api-version={API}", "--subscription", subscription)
        if (common.normalized(common.field(result, "id", "policy resource")) != common.normalized(resource_id)
                or not matches(result, plan["body"])):
            raise ValueError(f"Policy verification failed: {resource_id}")
        print(f"Verified policy resource: {resource_id}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "what-if", "create"))
    parser.add_argument("--inventory-directory", type=Path, default=Path("./.inventory"))
    parser.add_argument("--resolver-map", type=Path, required=True)
    parser.add_argument("--tenant-id", type=lambda value: common.guid(value, "tenant"),
                        help="Limit policy deployment to one inventoried tenant; default is all tenants.")
    parser.add_argument("--update-existing", action="store_true",
                        help="Allow explicit refresh of this lab's assignments when expected DNS settings change.")
    args = parser.parse_args(argv)
    try:
        documents = common.discover(args.inventory_directory)
        mapping = common.read_json(args.resolver_map)
        targets = common.validate_resolvers(mapping, documents)
        if len(targets) != len(documents):
            raise ValueError("Lab 04 requires exactly one resolver VNet per inventoried tenant")
        selected = [target for target in targets if args.tenant_id is None or target["tenantId"] == args.tenant_id]
        if not selected:
            raise ValueError("No inventoried tenant matches --tenant-id")
        options = {common.normalized(entry["resolverVnetId"]): entry for entry in mapping["resolvers"]}
        scopes = {
            target["tenantId"]: sorted(common.guid(item["id"], "inventory subscription")
                                       for item in documents[target["tenantId"]]["subscriptions"])
            for target in selected
        }
        for target in selected:
            for subscription in sorted(set(scopes[target["tenantId"]]) | {target["subscriptionId"]}):
                common.current_account(target["tenantId"], subscription, human=True)
                common.live_subscription(subscription, target["tenantId"])
        plans = []
        endpoints = []
        print("Script-generated Azure Policy preview (not ARM what-if):")
        for target in selected:
            alignment.read_vnet(target["resolverVnetId"])
            endpoint = alignment.inbound_endpoint(target, options[common.normalized(target["resolverVnetId"])])
            endpoints.append((target, endpoint))
            for subscription in scopes[target["tenantId"]]:
                check_alias(subscription)
                scope = common.subscription_scope(subscription)
                definition_id = f"{scope}/providers/Microsoft.Authorization/policyDefinitions/{NAME}"
                assignment_id = f"{scope}/providers/Microsoft.Authorization/policyAssignments/{NAME}"
                scope_plans = [
                    plan_resource(definition_id, definition(), subscription, args.update_existing),
                    plan_resource(assignment_id, assignment(definition_id, target, endpoint),
                                  subscription, args.update_existing),
                ]
                plans.extend(scope_plans)
                print(f"  Tenant {target['tenantId']}; subscription {subscription}")
                print(f"    Required ordered DNS: {endpoint['dns']}; excluded resolver: {target['resolverVnetId']}")
                for plan in scope_plans:
                    print(f"    {plan['action']}: {plan['id']}")
                    if plan["action"] == "Update":
                        previous = plan["previous"]["properties"]
                        print(f"      Previous parameters: {json.dumps(previous.get('parameters'))}")
                        print(f"      Previous enforcement mode: {previous.get('enforcementMode')}")
        print("Audit covers ALL VNets under each assignment, including unpeered and non-inventoried VNets. "
              "No DNS settings, identities, or role assignments are changed. "
              "DoNotEnforce evaluates compliance without enforcement; results are asynchronous.")
        changes = [plan for plan in plans if plan["action"] != "Keep"]
        if args.action == "create" and changes:
            common.confirm("dns-policy", ",".join(sorted(scopes)))
            apply(plans, endpoints)
    except (OSError, ValueError, EOFError) as exc:
        print(f"Error: {exc or 'confirmation input ended'}", file=sys.stderr)
        print("Stopped. Earlier successful policy changes are not rolled back; review printed resource IDs.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

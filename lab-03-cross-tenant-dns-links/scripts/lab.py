#!/usr/bin/env python3
"""Prepare Azure Lighthouse access and add CSV-selected cross-tenant DNS links."""

import argparse
import base64
import csv
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
DNS_ROLE = "b12aa53e-6015-4669-85d0-8515ebb3ae7f"
NETWORK_ROLE = "4d97b98b-1d4f-4787-a291-c67834d212e7"
MANAGED_API = "2022-10-01"


def helper(name, path):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inventory = helper("lab03_inventory", "lab-01-inventory/scripts/report_dns_hub_links.py")
central = helper("lab03_central", "lab-02-central-dns-zones/scripts/deploy.py")


def normalized(value):
    return inventory.normalized_id(value)


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def object_value(value, context):
    return inventory.object_value(value, context)


def text(value, context):
    return inventory.text(value, context)


def guid(value, context):
    return inventory.guid(value, context)


def collection(value, context):
    return inventory.array(value, context)


def field(value, key, context):
    return text(object_value(value, context).get(key), f"{context}.{key}")


def az(*arguments, sensitive=False):
    result = subprocess.run(
        ["az", *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, check=False,
    )
    operation = " ".join(arguments[:3])
    if result.returncode:
        detail = "sensitive response suppressed" if sensitive else result.stderr.strip()
        raise ValueError(f"Azure CLI {operation} failed (exit {result.returncode}): {detail}")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        raise ValueError(f"Azure CLI {operation}: invalid JSON response") from None


def pages(url, subscription=None):
    original = urlsplit(url)
    seen = set()
    result = []
    while url:
        parsed = urlsplit(url)
        if (url in seen or parsed.scheme != "https" or parsed.netloc != original.netloc
                or parsed.path != original.path or parsed.fragment):
            raise ValueError("Invalid or cyclic pagination URL")
        seen.add(url)
        arguments = ["rest", "--method", "get", "--url", url]
        if subscription:
            arguments.extend(("--subscription", subscription))
        page = object_value(az(*arguments), "list response")
        result.extend(collection(page.get("value"), "list response.value"))
        url = page.get("nextLink") or page.get("@odata.nextLink")
        if url is not None:
            text(url, "nextLink")
    return result


def discover(directory, central_tenant):
    if not directory.is_dir():
        raise ValueError(f"Inventory directory does not exist: {directory}")
    sources = sorted(directory.glob("*-inventory.json"))
    if not sources:
        raise ValueError(f"No *-inventory.json files found in {directory}")
    documents = {}
    subscriptions = {}
    for source in sources:
        document = read_json(source)
        inventory.validate_documents(document, document)
        tenant = guid(document["tenantId"], f"{source}.tenantId")
        if tenant in documents:
            raise ValueError(f"{source}: duplicate tenant inventory")
        for subscription in document["subscriptions"]:
            key = guid(subscription["id"], "subscription.id")
            if key in subscriptions and subscriptions[key] != tenant:
                raise ValueError(f"{source}: subscription occurs in different tenants")
            subscriptions[key] = tenant
        for key in ("virtualNetworks", "privateDnsZones"):
            ids = [normalized(item["id"]) for item in document[key]]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{source}: duplicate {key} resource IDs")
        documents[tenant] = document
    return {tenant: doc for tenant, doc in documents.items() if tenant != central_tenant}


def resolver_targets(path, documents):
    document = object_value(read_json(path), "resolver mapping")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
        raise ValueError("Resolver mapping: unsupported schemaVersion")
    targets = []
    seen = set()
    for entry in collection(document.get("resolvers"), "resolvers"):
        tenant = guid(field(entry, "tenantId", "resolver"), "resolver.tenantId")
        subscription = guid(field(entry, "subscriptionId", "resolver"), "resolver.subscriptionId")
        resource_id = field(entry, "resolverVnetId", "resolver")
        parts = inventory.identity({"name": "resolver", "id": resource_id}, inventory.VNET, "resolver")
        if guid(parts["subscription"], "resolver subscription") != subscription:
            raise ValueError("Resolver mapping subscription disagrees with VNet ID")
        if tenant not in documents:
            raise ValueError(f"Resolver mapping contains an undiscovered or central tenant: {tenant}")
        networks = {normalized(v["id"]): v for v in documents[tenant]["virtualNetworks"]}
        key = normalized(resource_id)
        if key not in networks:
            raise ValueError(f"Resolver VNet is absent from its tenant inventory: {resource_id}")
        if key in seen:
            raise ValueError(f"Duplicate resolver VNet mapping: {resource_id}")
        seen.add(key)
        targets.append({
            "tenantId": tenant, "subscriptionId": subscription,
            "resolverVnetId": networks[key]["id"].rstrip("/"),
            "resourceGroup": parts["group"],
        })
    if {entry["tenantId"] for entry in targets} != set(documents):
        raise ValueError("Resolver mapping must cover every discovered non-central tenant")
    return sorted(targets, key=lambda entry: normalized(entry["resolverVnetId"]))


def subscription_scope(subscription):
    return f"/subscriptions/{subscription}"


def group_scope(subscription, group):
    return f"{subscription_scope(subscription)}/resourceGroups/{group}"


def confirm(action, scope):
    expected = f"{action}:{scope}"
    if input(f"Type '{expected}' to apply these changes: ") != expected:
        raise ValueError("Cancelled: confirmation did not match")


def current_account(tenant, subscription=None, principal=None, human=False):
    arguments = ["account", "show"]
    if subscription:
        arguments.extend(("--subscription", subscription))
    account = object_value(az(*arguments), "account")
    if guid(account.get("tenantId"), "signed-in tenant") != tenant:
        raise ValueError("Wrong signed-in tenant; sign in explicitly to the requested tenant")
    user = object_value(account.get("user"), "signed-in user")
    if human and user.get("type") != "user":
        raise ValueError("This setup workflow requires an authenticated human operator")
    if principal:
        if user.get("type") not in ("user", "servicePrincipal"):
            raise ValueError("Sign in as the central human operator or configured managed identity")
        token = az("account", "get-access-token", "--subscription", subscription,
                   "--query", "accessToken", sensitive=True)
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            token_tenant = guid(claims.get("tid"), "token tenant")
            token_object = guid(claims.get("oid"), "token object ID")
        except (AttributeError, IndexError, ValueError):
            raise ValueError("Cannot inspect the Azure CLI token identity") from None
        if token_tenant != tenant:
            raise ValueError("Azure CLI token does not belong to the central tenant")
        if user.get("type") == "servicePrincipal" and token_object != guid(principal["objectId"], "managed identity object ID"):
            raise ValueError("Azure CLI token does not belong to the configured central managed identity")


def live_subscription(subscription, tenant):
    result = az("rest", "--method", "get", "--url",
                f"{ARM}/subscriptions/{subscription}?api-version=2022-12-01",
                "--subscription", subscription)
    if guid(field(result, "tenantId", "subscription"), "subscription.tenantId") != tenant:
        raise ValueError(f"Live subscription belongs to a different tenant: {subscription}")
    if result.get("state") != "Enabled":
        raise ValueError(f"Subscription is not Enabled: {subscription}")


def live_group(subscription, group):
    result = az("group", "show", "--subscription", subscription, "--name", group)
    expected = group_scope(subscription, group)
    if normalized(field(result, "id", "resource group")) != normalized(expected):
        raise ValueError(f"Resource group lookup returned an unexpected ID: {expected}")
    return expected


def permissions(scope, actions, subscription=None):
    grants = pages(f"{ARM}{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01",
                   subscription=subscription)
    for action in actions:
        allowed = any(
            any(fnmatch.fnmatchcase(action.casefold(), pattern.casefold())
                for pattern in collection(grant.get("actions"), "permissions.actions"))
            and not any(fnmatch.fnmatchcase(action.casefold(), pattern.casefold())
                        for pattern in collection(grant.get("notActions"), "permissions.notActions"))
            for grant in grants
        )
        if not allowed:
            raise ValueError(f"Missing effective permission {action} at {scope}")


def check_resolvers(targets):
    for target in targets:
        subscription = target["subscriptionId"]
        live_subscription(subscription, target["tenantId"])
        result = az("network", "vnet", "show", "--subscription", subscription,
                    "--ids", target["resolverVnetId"])
        if normalized(field(result, "id", "VNet")) != normalized(target["resolverVnetId"]):
            raise ValueError("VNet lookup returned an unexpected ID")
        resolvers = pages(
            f"{ARM}{group_scope(subscription, target['resourceGroup'])}"
            "/providers/Microsoft.Network/dnsResolvers?api-version=2022-07-01",
            subscription=subscription,
        )
        matches = [resolver for resolver in resolvers if normalized(
            field(object_value(resolver.get("properties"), "resolver properties").get("virtualNetwork"),
                  "id", "resolver VNet")
        ) == normalized(target["resolverVnetId"])]
        if not matches or any(resolver["properties"].get("provisioningState") != "Succeeded"
                              for resolver in matches):
            raise ValueError(f"No healthy DNS Private Resolver in VNet: {target['resolverVnetId']}")


def validate_state(document, args):
    object_value(document, "identity state")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
        raise ValueError("Identity state: expected schemaVersion 2 (managed identity only). "
                         "Preserve old state for cleanup and run identities with a new state-file path.")
    if "servicePrincipal" in document:
        raise ValueError("Identity state must contain only the managed identity; review legacy state manually")
    if guid(document.get("centralTenantId"), "identity state tenant") != args.central_tenant_id:
        raise ValueError("Identity state belongs to another central tenant")
    subscription = guid(document.get("centralSubscriptionId"), "identity state subscription")
    group = text(document.get("dnsResourceGroup"), "identity state DNS resource group")
    if getattr(args, "central_subscription_id", subscription) != subscription or (
        getattr(args, "dns_resource_group", group).casefold() != group.casefold()
    ):
        raise ValueError("Identity state belongs to another DNS destination")
    principal = object_value(document.get("managedIdentity"), "managedIdentity")
    guid(principal.get("clientId"), "managedIdentity.clientId")
    guid(principal.get("objectId"), "managedIdentity.objectId")
    identity_id = field(document["managedIdentity"], "resourceId", "managedIdentity")
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/[^/]+/providers/"
        r"Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+", identity_id, re.IGNORECASE,
    )
    if not match or guid(match[1], "managed identity subscription") != subscription:
        raise ValueError("Managed identity resource ID does not match the central subscription")
    return document


def private_file(path):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if os.name == "nt":
            result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                    check=True, capture_output=True, text=True)
            sid = next(csv.reader([result.stdout.strip()]))[1]
            if not re.fullmatch(r"S-1-[0-9-]+", sid):
                raise ValueError("Cannot determine Windows SID for secure file storage")
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                           check=True, capture_output=True, text=True)
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError):
        os.close(descriptor)
        path.unlink()
        raise ValueError("Cannot establish owner-only file permissions") from None


def save_state(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".lab03-{uuid4()}.json"
    try:
        with private_file(temporary) as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def find_one(values, context):
    values = collection(values, context)
    if len(values) > 1:
        raise ValueError(f"Ambiguous {context}; use a unique lab identity name")
    return values[0] if values else None


def identity_setup(args):
    old_state = validate_state(read_json(args.state_file), args) if args.state_file.exists() else None
    current_account(args.central_tenant_id, args.central_subscription_id, human=True)
    live_subscription(args.central_subscription_id, args.central_tenant_id)
    scope = live_group(args.central_subscription_id, args.dns_resource_group)
    live_group(args.central_subscription_id, args.identity_resource_group)
    permissions(scope, ["Microsoft.Authorization/roleAssignments/write"],
                subscription=args.central_subscription_id)
    identities = az("identity", "list", "--subscription", args.central_subscription_id,
                    "--resource-group", args.identity_resource_group)
    identity = find_one([entry for entry in collection(identities, "identities")
                         if entry.get("name", "").casefold() == args.name.casefold()], "managed identity")
    if old_state:
        if not identity or any((
            normalized(identity.get("principalId", "")) != old_state["managedIdentity"]["objectId"],
            normalized(identity.get("id", "")) != normalized(old_state["managedIdentity"]["resourceId"]),
        )):
            raise ValueError("Existing identity state conflicts with the selected identities")
    print("Script-generated identity/RBAC preview (not ARM what-if):")
    print(f"  Managed identity: {args.name} ({'reuse' if identity else 'create'})")
    print(f"  Managed identity: Private DNS Zone Contributor on {scope}")
    if args.action != "create":
        return
    confirm("identities", scope)
    created = list(old_state.get("createdResourceIds", [])) if old_state else []
    if not identity:
        identity = az("identity", "create", "--subscription", args.central_subscription_id,
                      "--resource-group", args.identity_resource_group, "--name", args.name,
                      "--location", args.location)
        created.append(field(identity, "id", "managedIdentity"))
        print(f"Created managed identity: {identity['id']}")
    state = {
        "schemaVersion": 2, "centralTenantId": args.central_tenant_id,
        "centralSubscriptionId": args.central_subscription_id, "dnsResourceGroup": args.dns_resource_group,
        "managedIdentity": {
            "clientId": guid(identity.get("clientId"), "managedIdentity.clientId"),
            "objectId": guid(identity.get("principalId"), "managedIdentity.principalId"),
            "resourceId": field(identity, "id", "managedIdentity"),
        },
        "createdResourceIds": created,
    }
    validate_state(state, args)
    save_state(args.state_file, state)
    principal = state["managedIdentity"]
    assignments = az("role", "assignment", "list", "--subscription", args.central_subscription_id,
                     "--scope", scope, "--fill-principal-name", "false")
    exists = any(normalized(entry.get("principalId", "")) == principal["objectId"]
                 and normalized(entry.get("roleDefinitionId", "")).endswith("/" + DNS_ROLE)
                 and normalized(entry.get("scope", "")) == normalized(scope)
                 for entry in collection(assignments, "role assignments"))
    if not exists:
        assignment = az(
            "role", "assignment", "create", "--subscription", args.central_subscription_id,
            "--name", str(uuid5(NAMESPACE_URL, f"{normalized(scope)}/{principal['objectId']}/{DNS_ROLE}")),
            "--assignee-object-id", principal["objectId"], "--assignee-principal-type", "ServicePrincipal",
            "--role", DNS_ROLE, "--scope", scope,
        )
        state["createdResourceIds"].append(field(assignment, "id", "role assignment"))
        save_state(args.state_file, state)
        print(f"Created role assignment: {assignment['id']}")
    print(f"Identity state saved to {args.state_file}. No credentials are stored in that state.")


def guests(args, documents):
    if args.tenant_id not in documents:
        raise ValueError("Guest destination must be a discovered non-central tenant")
    current_account(args.tenant_id, human=True)
    address = args.user_email.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", address):
        raise ValueError("Expected the existing central user's invitation email address")
    escaped = address.replace("'", "''")
    query = urlencode({
        "$filter": f"userType eq 'Guest' and (mail eq '{escaped}' or otherMails/any(m:m eq '{escaped}'))",
        "$select": "id,userType,externalUserState,mail,otherMails",
        "$count": "true",
    })
    # Advanced directory queries require eventual consistency on every page.
    url = f"{GRAPH}/users?{query}"
    matches = []
    seen = set()
    while url:
        parsed = urlsplit(url)
        if url in seen or parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or parsed.path != "/v1.0/users":
            raise ValueError("Invalid or cyclic guest pagination URL")
        seen.add(url)
        response = object_value(az("rest", "--method", "get", "--url", url,
                                   "--headers", "ConsistencyLevel=eventual"), "guest response")
        matches.extend(collection(response.get("value"), "guests"))
        url = response.get("@odata.nextLink")
    guest = find_one(matches, "guest email")
    if guest:
        print(f"Existing guest object: {guid(guest.get('id'), 'guest.id')}; "
              f"redemption: {guest.get('externalUserState', 'unknown')}. No invitation sent.")
        return
    print(f"Script-generated guest invitation preview: one invitation in tenant {args.tenant_id}.")
    print("The user must accept the invitation; Azure RBAC is a separate administrator step.")
    if args.action == "create":
        confirm("guest", args.tenant_id)
        result = az(
            "rest", "--method", "post", "--url", f"{GRAPH}/invitations", "--body",
            json.dumps({"invitedUserEmailAddress": address, "inviteRedirectUrl": "https://portal.azure.com",
                        "sendInvitationMessage": True}),
        )
        user = object_value(object_value(result, "invitation").get("invitedUser"), "invited user")
        print(f"Invitation sent; guest object: {guid(user.get('id'), 'invitedUser.id')}. Await acceptance.")


def delegation_names(scope, tenant):
    digest = hashlib.sha256(f"{normalized(scope)}/{tenant}".encode()).hexdigest()[:20]
    return str(uuid5(NAMESPACE_URL, f"lab03/{normalized(scope)}/{tenant}")), f"dns-lab03-{digest}"


def delegations(args, targets, state):
    selected = [target for target in targets if target["tenantId"] == args.tenant_id]
    if not selected:
        raise ValueError("Delegation destination must be a mapped non-central tenant")
    for subscription in sorted({target["subscriptionId"] for target in selected}):
        current_account(args.tenant_id, subscription, human=True)
    check_resolvers(selected)
    scopes = {group_scope(target["subscriptionId"], target["resourceGroup"]): target for target in selected}
    authorizations = [{
        "principalId": state["managedIdentity"]["objectId"],
        "principalIdDisplayName": "Lab03 managedIdentity",
        "roleDefinitionId": NETWORK_ROLE,
    }]
    pending = []
    for scope, target in scopes.items():
        subscription = target["subscriptionId"]
        live_group(subscription, target["resourceGroup"])
        provider = az("provider", "show", "--subscription", subscription, "--namespace", "Microsoft.ManagedServices")
        if provider.get("registrationState") != "Registered":
            raise ValueError(f"Register Microsoft.ManagedServices in {subscription} before onboarding")
        permissions(subscription_scope(subscription), [
            "Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/write",
            "Microsoft.Authorization/roleAssignments/delete",
        ], subscription=subscription)
        registration, offer = delegation_names(scope, args.central_tenant_id)
        definitions_scope = f"{subscription_scope(subscription)}/providers/Microsoft.ManagedServices"
        assignments_scope = f"{scope}/providers/Microsoft.ManagedServices"
        definitions = pages(f"{ARM}{definitions_scope}/registrationDefinitions?api-version={MANAGED_API}",
                            subscription=subscription)
        assignments = pages(f"{ARM}{assignments_scope}/registrationAssignments?api-version={MANAGED_API}",
                            subscription=subscription)
        definition = find_one([item for item in definitions if item.get("name") == registration],
                              "Lighthouse definition")
        assignment = find_one([item for item in assignments if item.get("name") == registration],
                              "Lighthouse assignment")
        expected_id = f"{definitions_scope}/registrationDefinitions/{registration}"
        if definition:
            properties = object_value(definition.get("properties"), "Lighthouse definition")
            actual = sorted((guid(entry.get("principalId"), "authorization principal"),
                             guid(entry.get("roleDefinitionId"), "authorization role"))
                            for entry in collection(properties.get("authorizations"), "authorizations"))
            expected = sorted((entry["principalId"], entry["roleDefinitionId"]) for entry in authorizations)
            if (properties.get("managedByTenantId", "").casefold() != args.central_tenant_id
                    or properties.get("registrationDefinitionName") != offer or actual != expected
                    or properties.get("eligibleAuthorizations")):
                raise ValueError(f"Conflicting existing Lighthouse definition at {scope}. "
                                 "Review and explicitly remove any legacy dual-identity delegation before retrying.")
            if properties.get("provisioningState") != "Succeeded":
                raise ValueError(f"Existing Lighthouse definition is not healthy at {scope}")
        if assignment:
            properties = object_value(assignment.get("properties"), "Lighthouse assignment")
            if not definition or normalized(properties.get("registrationDefinitionId", "")) != normalized(expected_id):
                raise ValueError(f"Conflicting existing Lighthouse assignment at {scope}")
            if properties.get("provisioningState") != "Succeeded":
                raise ValueError(f"Existing Lighthouse assignment is not healthy at {scope}")
        print(f"{'Keep' if assignment else 'Delegate'} Network Contributor to the managed identity "
              f"on ENTIRE RESOURCE GROUP: {scope}")
        if not assignment:
            location = text(az("group", "show", "--subscription", subscription,
                               "--name", target["resourceGroup"], "--query", "location"),
                            "resolver resource group location")
            print(f"  Subscription deployment metadata location: {location}")
            pending.append((target, registration, offer, location))
    if not pending:
        print("All requested delegations already exist; no changes.")
        return
    if args.action == "create":
        confirm("delegate", args.tenant_id)
    for target, registration, offer, location in pending:
        parameters = {
            "managedByTenantId": {"value": args.central_tenant_id},
            "registrationName": {"value": registration}, "offerName": {"value": offer},
            "authorizations": {"value": authorizations},
            "resolverResourceGroup": {"value": target["resourceGroup"]},
        }
        with tempfile.TemporaryDirectory(prefix="dns-lab03-") as directory:
            path = Path(directory) / "parameters.json"
            path.write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
            output_arguments = ["--no-pretty-print"] if args.action == "what-if" else []
            az("deployment", "sub", args.action, "--subscription", target["subscriptionId"],
               "--location", location, "--name", offer,
               "--template-file", str(ROOT / "lighthouse.bicep"), "--parameters", f"@{path}",
               *output_arguments)
        if args.action == "create":
            print(f"Created delegation: {group_scope(target['subscriptionId'], target['resourceGroup'])}"
                  f"/providers/Microsoft.ManagedServices/registrationAssignments/{registration}")


def noncentral_link_replacements(pending, documents):
    if not pending:
        return []
    requested = {(link["zone"], normalized(link["resolverVnetId"])) for link in pending}
    names = {name for name, _ in requested}
    replacements = []
    for tenant in sorted(documents):
        for subscription in documents[tenant]["subscriptions"]:
            subscription_id = guid(subscription["id"], "non-central subscription")
            live_subscription(subscription_id, tenant)
            zones = pages(
                f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Network/"
                "privateDnsZones?api-version=2018-09-01", subscription=subscription_id,
            )
            for zone in zones:
                name = field(zone, "name", "non-central zone")
                selected_name = name.rstrip(".").casefold()
                if selected_name not in names:
                    continue
                parts = inventory.identity(zone, inventory.ZONE, "non-central zone")
                zone_id = field(zone, "id", "non-central zone").rstrip("/")
                if (guid(parts["subscription"], "zone subscription") != subscription_id
                        or zone_id.rsplit("/", 1)[-1].casefold() != name.casefold()):
                    raise ValueError(f"Non-central zone lookup returned an unexpected ID: {zone_id}")
                links = collection(az(
                    "network", "private-dns", "link", "vnet", "list",
                    "--subscription", subscription_id, "--resource-group", parts["group"],
                    "--zone-name", name,
                ), "non-central VNet links")
                for link in links:
                    vnet = field(link.get("virtualNetwork"), "id", "non-central link VNet")
                    if (selected_name, normalized(vnet)) not in requested:
                        continue
                    inventory.identity(link, re.escape(zone_id) + r"/virtualNetworkLinks/[^/]+",
                                       "non-central link")
                    link_name = field(link, "name", "non-central link")
                    link_id = field(link, "id", "non-central link")
                    if normalized(link_id) != normalized(f"{zone_id}/virtualNetworkLinks/{link_name}"):
                        raise ValueError(f"Non-central link lookup returned an unexpected ID: {link_id}")
                    if link.get("provisioningState") != "Succeeded":
                        raise ValueError(f"Conflicting non-central link is not healthy: {link_id}")
                    inventory.boolean(link.get("registrationEnabled"), "non-central link registrationEnabled")
                    etag = field(link, "etag", "non-central link")
                    permissions(link_id, ["Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
                                          "Microsoft.Network/privateDnsZones/virtualNetworkLinks/delete"],
                                subscription=subscription_id)
                    replacements.append({
                        "id": link_id, "zone": selected_name, "zoneName": name, "name": link_name,
                        "subscriptionId": subscription_id, "resourceGroup": parts["group"],
                        "resolverVnetId": vnet, "etag": etag,
                        "registrationEnabled": link["registrationEnabled"],
                    })
    return replacements


def remove_noncentral_link(link):
    az("network", "private-dns", "link", "vnet", "delete",
       "--subscription", link["subscriptionId"], "--resource-group", link["resourceGroup"],
       "--zone-name", link["zoneName"], "--name", link["name"],
       "--if-match", link["etag"], "--yes")
    remaining = collection(az(
        "network", "private-dns", "link", "vnet", "list",
        "--subscription", link["subscriptionId"], "--resource-group", link["resourceGroup"],
        "--zone-name", link["zoneName"],
    ), "remaining non-central links")
    if any(normalized(field(item, "id", "remaining link")) == normalized(link["id"]) for item in remaining):
        raise ValueError(f"Non-central link deletion verification failed: {link['id']}")
    print(f"Removed and verified non-central link: {link['id']}", flush=True)


def link_zones(args, targets, state, documents):
    _, selected = central.load_zone_names(args.input_csv, args.central_tenant_id)
    if not selected:
        print("CSV contains no selected zones; no link lookups or changes.")
        return
    subscription = state["centralSubscriptionId"]
    group = state["dnsResourceGroup"]
    current_account(args.central_tenant_id, subscription, principal=state["managedIdentity"])
    live_subscription(subscription, args.central_tenant_id)
    scope = live_group(subscription, group)
    permissions(scope, ["Microsoft.Network/privateDnsZones/read",
                        "Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
                        "Microsoft.Network/privateDnsZones/virtualNetworkLinks/write"], subscription=subscription)
    check_resolvers(targets)
    for target in targets:
        permissions(target["resolverVnetId"], ["Microsoft.Network/virtualNetworks/read",
                                             "Microsoft.Network/virtualNetworks/join/action"],
                    subscription=target["subscriptionId"])
    pending = []
    for zone in sorted(selected):
        resource = az("network", "private-dns", "zone", "show", "--subscription", subscription,
                      "--resource-group", group, "--name", zone)
        expected = f"{scope}/providers/Microsoft.Network/privateDnsZones/{zone}"
        if normalized(field(resource, "id", "zone")) != normalized(expected):
            raise ValueError(f"Unexpected zone lookup ID for {zone}")
        links = collection(az("network", "private-dns", "link", "vnet", "list",
                              "--subscription", subscription, "--resource-group", group,
                              "--zone-name", zone), "VNet links")
        by_name = {}
        for link in links:
            name = field(link, "name", "link")
            central.vnet_id(field(link.get("virtualNetwork"), "id", "link.virtualNetwork").rstrip("/"))
            if name.casefold() in by_name:
                raise ValueError(f"Duplicate Azure link names in {zone}")
            by_name[name.casefold()] = link
        for target in targets:
            resource_id = target["resolverVnetId"]
            matching = [link for link in links if normalized(link["virtualNetwork"]["id"]) == normalized(resource_id)]
            if matching:
                if len(matching) != 1 or matching[0].get("provisioningState") != "Succeeded":
                    raise ValueError(f"Existing resolver link is not healthy: {zone} -> {resource_id}")
                print(f"Keep existing link: {zone}/{matching[0]['name']} -> {resource_id}")
                continue
            name = "resolver-" + hashlib.sha256(normalized(resource_id).encode()).hexdigest()[:24]
            if name in by_name:
                raise ValueError(f"Link name collision in {zone}: {name}; existing link will not be overwritten")
            pending.append({"zone": zone, "name": name, **target})
    replacements = noncentral_link_replacements(pending, documents)
    print(f"Script-generated link preview (not ARM what-if): {len(pending)} to create; "
          f"{len(replacements)} non-central links to delete.")
    for link in replacements:
        print(f"  - {link['id']} -> {link['resolverVnetId']} "
              f"(registrationEnabled={link['registrationEnabled']})")
    for link in pending:
        print(f"  + {scope}/providers/Microsoft.Network/privateDnsZones/{link['zone']}"
              f"/virtualNetworkLinks/{link['name']} -> {link['resolverVnetId']} (tenant {link['tenantId']})")
    if replacements:
        print("WARNING: Replacing local links can interrupt DNS resolution. Inventory A-record exports "
              "are not migrated records; verify the central zones before confirming. Removing an "
              "auto-registration-enabled link also deletes its auto-registered records. "
              "Deleted links are not automatically restored if central link creation fails.")
    if args.action != "create" or not pending:
        return
    confirm("links", f"{args.central_tenant_id}/{subscription}/{group}")
    for index, link in enumerate(pending, 1):
        for replacement in replacements:
            if (replacement["zone"] == link["zone"]
                    and normalized(replacement["resolverVnetId"]) == normalized(link["resolverVnetId"])):
                remove_noncentral_link(replacement)
        result = az("network", "private-dns", "link", "vnet", "create",
                    "--subscription", subscription, "--resource-group", group, "--zone-name", link["zone"],
                    "--name", link["name"], "--virtual-network", link["resolverVnetId"],
                    "--registration-enabled", "false")
        if (field(result.get("virtualNetwork"), "id", "created link").casefold() != link["resolverVnetId"].casefold()
                or result.get("provisioningState") != "Succeeded"):
            raise ValueError(f"Created link verification failed: {link['zone']}/{link['name']}")
        print(f"Created and verified ({index}/{len(pending)}): {link['zone']}/{link['name']}", flush=True)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="workflow", required=True)
    for workflow in ("discover", "guests", "identities", "delegate", "links"):
        command = commands.add_parser(workflow)
        command.add_argument("--central-tenant-id", required=True, type=lambda value: guid(value, "central tenant"))
        if workflow != "identities":
            command.add_argument("--inventory-directory", type=Path, default=Path("./.inventory"))
        if workflow != "discover":
            command.add_argument("action", choices=("validate", "what-if", "create"))
        if workflow in ("delegate", "links"):
            command.add_argument("--resolver-map", type=Path, required=True)
        if workflow in ("identities", "delegate", "links"):
            command.add_argument("--state-file", type=Path, required=True)
        if workflow in ("guests", "delegate"):
            command.add_argument("--tenant-id", required=True, type=lambda value: guid(value, "destination tenant"))
        if workflow == "guests":
            command.add_argument("--user-email", required=True)
        if workflow == "identities":
            command.add_argument("--central-subscription-id", required=True,
                                 type=lambda value: guid(value, "central subscription"))
            command.add_argument("--dns-resource-group", required=True)
            command.add_argument("--identity-resource-group", required=True)
            command.add_argument("--location", required=True)
            command.add_argument("--name", required=True)
        if workflow == "links":
            command.add_argument("--input-csv", type=Path, default=Path("./.inventory/hub-dns-zones.csv"))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.workflow == "identities":
            for key in ("name", "dns_resource_group", "identity_resource_group", "location"):
                value = text(getattr(args, key), key)
                if not re.fullmatch(r"[a-zA-Z0-9_.()-]+", value) or value.startswith("-"):
                    raise ValueError(f"Invalid {key}")
            identity_setup(args)
            return 0
        documents = discover(args.inventory_directory, args.central_tenant_id)
        if args.workflow == "discover":
            print(json.dumps({
                "schemaVersion": 1,
                "tenants": [{"tenantId": tenant, "suggestedHubVnetId": doc.get("hubVnetId"),
                             "subscriptions": [sub["id"] for sub in doc["subscriptions"]]}
                            for tenant, doc in sorted(documents.items())],
            }, indent=2))
            return 0
        if args.workflow == "guests":
            guests(args, documents)
            return 0
        targets = resolver_targets(args.resolver_map, documents)
        state = validate_state(read_json(args.state_file), args)
        if not targets:
            print("No non-central tenants or resolver targets; no changes.")
            return 0
        if args.workflow == "delegate":
            delegations(args, targets, state)
        else:
            link_zones(args, targets, state, documents)
    except EOFError:
        print("Error: Cancelled: confirmation input ended.", file=sys.stderr)
        return 1
    except (OSError, ValueError, csv.Error, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Stopped. Earlier successful changes are not rolled back; review the progress output.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

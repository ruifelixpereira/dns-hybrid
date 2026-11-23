import argparse
import base64
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cross_tenant_lab", ROOT / "scripts/lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
REMOTE = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
OBJECT = "44444444-4444-4444-4444-444444444444"
USER = "55555555-5555-5555-5555-555555555555"
GROUP = "central-dns"
IDENTITY = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/lab03"
VNET = f"/subscriptions/{REMOTE}/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver"
TARGET = {"tenantId": OTHER, "subscriptionId": REMOTE, "resourceGroup": "network", "resolverVnetId": VNET}
DOCUMENTS = {OTHER: {"subscriptions": [{"id": REMOTE}]}}


def state():
    return {
        "schemaVersion": 2, "centralTenantId": TENANT,
        "centralSubscriptionId": SUBSCRIPTION, "dnsResourceGroup": GROUP,
        "managedIdentity": {"clientId": CLIENT, "objectId": OBJECT, "resourceId": IDENTITY},
        "createdResourceIds": [],
    }


def token(tenant=TENANT, object_id=OBJECT):
    payload = base64.urlsafe_b64encode(json.dumps({"tid": tenant, "oid": object_id}).encode())
    return "header." + payload.decode().rstrip("=") + ".signature"


class LabTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.args = argparse.Namespace(
            central_tenant_id=TENANT, central_subscription_id=SUBSCRIPTION,
            dns_resource_group=GROUP, identity_resource_group="identities",
            name="lab03", location="westeurope", action="create", tenant_id=OTHER,
            state_file=directory / "state.json", input_csv=directory / "zones.csv",
        )

    def mock(self, name, **kwargs):
        return self.stack.enter_context(patch.object(lab, name, **kwargs))

    def caller(self, user_type, claims, name="userAssignedIdentity"):
        def az(*args, **kwargs):
            if args[:2] == ("account", "show"):
                return {"tenantId": TENANT, "user": {"type": user_type, "name": name}}
            if args[:2] == ("account", "get-access-token"):
                self.assertTrue(kwargs["sensitive"])
                self.assertIn(SUBSCRIPTION, args)
                return claims
            self.fail(f"Unexpected Azure CLI call: {args}")
        return self.mock("az", side_effect=az)

    def test_accepts_central_human_and_configured_managed_identity(self):
        for user_type, object_id in (("user", USER), ("servicePrincipal", OBJECT)):
            with self.subTest(user_type=user_type):
                mock = self.caller(user_type, token(object_id=object_id))
                lab.current_account(TENANT, SUBSCRIPTION, principal=state()["managedIdentity"])
                self.assertEqual(mock.call_count, 2)

    def test_rejects_other_application_tokens_and_wrong_tenant(self):
        cases = (
            ("servicePrincipal", token(object_id=USER), "configured central managed identity"),
            ("user", token(tenant=OTHER, object_id=USER), "central tenant"),
            ("servicePrincipal", token(tenant=OTHER), "central tenant"),
            ("user", token(object_id=None), "Cannot inspect"),
            ("user", "not-a-token", "Cannot inspect"),
        )
        for user_type, claims, message in cases:
            with self.subTest(user_type=user_type, claims=claims):
                self.caller(user_type, claims)
                with self.assertRaisesRegex(ValueError, message):
                    lab.current_account(TENANT, SUBSCRIPTION, principal=state()["managedIdentity"])

    def test_setup_still_requires_human_and_correct_account_tenant(self):
        self.caller("servicePrincipal", token())
        with self.assertRaisesRegex(ValueError, "human operator"):
            lab.current_account(TENANT, SUBSCRIPTION, human=True)
        with self.assertRaisesRegex(ValueError, "Wrong signed-in tenant"):
            lab.current_account(OTHER, SUBSCRIPTION, principal=state()["managedIdentity"])

    def test_rejects_legacy_state_before_any_azure_calls(self):
        legacy = {**state(), "schemaVersion": 1, "servicePrincipal": {"objectId": USER}}
        self.args.state_file.write_text(json.dumps(legacy))
        az = self.mock("az")
        with self.assertRaisesRegex(ValueError, "new state-file path"):
            lab.identity_setup(self.args)
        az.assert_not_called()
        self.assertEqual(json.loads(self.args.state_file.read_text()), legacy)
        with self.assertRaisesRegex(ValueError, "only the managed identity"):
            lab.validate_state({**legacy, "schemaVersion": 2}, self.args)

    def test_identity_state_requires_managed_identity_in_central_subscription(self):
        self.assertEqual(lab.validate_state(state(), self.args), state())
        for mutation in ({"objectId": "invalid"}, {"resourceId": IDENTITY.replace(SUBSCRIPTION, REMOTE)}):
            with self.subTest(mutation=mutation):
                invalid = state()
                invalid["managedIdentity"].update(mutation)
                with self.assertRaises(ValueError):
                    lab.validate_state(invalid, self.args)

    def identity_calls(self, existing=False, missing_role=False):
        scope = lab.group_scope(SUBSCRIPTION, GROUP)
        identity = {"name": "lab03", "id": IDENTITY, "clientId": CLIENT, "principalId": OBJECT}
        assignment = {
            "id": scope + "/providers/Microsoft.Authorization/roleAssignments/assignment",
            "scope": scope, "principalId": OBJECT,
            "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{lab.DNS_ROLE}",
        }

        def az(*args, **kwargs):
            if args[:2] == ("identity", "list"):
                return [identity] if existing else []
            if args[:2] == ("identity", "create"):
                return identity
            if args[:3] == ("role", "assignment", "list"):
                self.assertEqual(args[args.index("--scope") + 1], scope)
                self.assertNotIn("--all", args)
                self.assertEqual(args[args.index("--fill-principal-name") + 1], "false")
                return [assignment] if existing and not missing_role else []
            if args[:3] == ("role", "assignment", "create"):
                self.assertEqual(args[args.index("--assignee-object-id") + 1], OBJECT)
                self.assertEqual(args[args.index("--scope") + 1], scope)
                return assignment
            self.fail(f"Unexpected Azure CLI call (no app/SP/credential operations allowed): {args}")

        self.mock("current_account")
        self.mock("live_subscription")
        self.mock("live_group", side_effect=lab.group_scope)
        self.mock("permissions")
        self.mock("confirm")
        return self.mock("az", side_effect=az), assignment

    def test_provisions_only_managed_identity_and_dns_role(self):
        az, assignment = self.identity_calls()
        lab.identity_setup(self.args)
        saved = lab.validate_state(lab.read_json(self.args.state_file), self.args)
        self.assertNotIn("servicePrincipal", saved)
        self.assertEqual(saved["managedIdentity"], state()["managedIdentity"])
        self.assertEqual(saved["createdResourceIds"], [IDENTITY, assignment["id"]])
        self.assertEqual([call.args[:2] for call in az.call_args_list],
                         [("identity", "list"), ("identity", "create"), ("role", "assignment"), ("role", "assignment")])

    def test_identity_rerun_reuses_identity_and_role_without_writes_to_azure(self):
        original = state()
        original["createdResourceIds"] = [IDENTITY]
        self.args.state_file.write_text(json.dumps(original))
        az, _ = self.identity_calls(existing=True)
        lab.identity_setup(self.args)
        self.assertEqual(lab.read_json(self.args.state_file), original)
        self.assertEqual([call.args[:3] for call in az.call_args_list],
                         [("identity", "list", "--subscription"), ("role", "assignment", "list")])

    def test_identity_rerun_completes_role_assignment_after_partial_failure(self):
        original = state()
        original["createdResourceIds"] = [IDENTITY]
        self.args.state_file.write_text(json.dumps(original))
        az, assignment = self.identity_calls(existing=True, missing_role=True)
        lab.identity_setup(self.args)
        self.assertEqual(lab.read_json(self.args.state_file)["createdResourceIds"],
                         [IDENTITY, assignment["id"]])
        self.assertEqual([call.args[:3] for call in az.call_args_list], [
            ("identity", "list", "--subscription"),
            ("role", "assignment", "list"),
            ("role", "assignment", "create"),
        ])

    def test_identity_preview_has_no_writes_or_confirmation(self):
        az, _ = self.identity_calls()
        self.args.action = "what-if"
        lab.identity_setup(self.args)
        self.assertEqual(az.call_count, 1)
        lab.confirm.assert_not_called()
        self.assertFalse(self.args.state_file.exists())

    def test_permission_requests_use_selected_subscription_context(self):
        az = self.mock("az", return_value={"value": [{"actions": ["*"], "notActions": []}]})
        lab.permissions(VNET, ["Microsoft.Network/virtualNetworks/join/action"], subscription=REMOTE)
        self.assertEqual(az.call_args.args[-2:], ("--subscription", REMOTE))
        az.return_value = {"value": []}
        with self.assertRaisesRegex(ValueError, "Missing effective permission"):
            lab.permissions(VNET, ["Microsoft.Network/virtualNetworks/join/action"], subscription=REMOTE)

    def test_subscription_and_resolver_rest_queries_select_remote_context(self):
        calls = []

        def az(*args):
            calls.append(args)
            if args[0] == "rest" and "dnsResolvers" not in args[args.index("--url") + 1]:
                return {"tenantId": OTHER, "state": "Enabled"}
            if args[0] == "rest":
                return {"value": [{"properties": {
                    "virtualNetwork": {"id": VNET}, "provisioningState": "Succeeded",
                }}]}
            if args[:3] == ("network", "vnet", "show"):
                return {"id": VNET}
            self.fail(f"Unexpected Azure CLI call: {args}")

        self.mock("az", side_effect=az)
        lab.check_resolvers([TARGET])
        self.assertEqual(len(calls), 3)
        for args in calls:
            self.assertEqual(args[args.index("--subscription") + 1], REMOTE)

    def delegation_calls(self, legacy=False, existing=False, wrong_definition_scope=False):
        scope = lab.group_scope(REMOTE, "network")
        registration, offer = lab.delegation_names(scope, TENANT)
        authorization = {"principalId": OBJECT, "principalIdDisplayName": "Lab03 managedIdentity",
                         "roleDefinitionId": lab.NETWORK_ROLE}
        definition = {"name": registration, "properties": {
            "managedByTenantId": TENANT, "registrationDefinitionName": offer,
            "provisioningState": "Succeeded",
            "authorizations": [authorization] + ([{**authorization, "principalId": USER}] if legacy else []),
        }}
        definition_scope = scope if wrong_definition_scope else lab.subscription_scope(REMOTE)
        assignment = {"name": registration, "properties": {
            "registrationDefinitionId": f"{definition_scope}/providers/Microsoft.ManagedServices/registrationDefinitions/{registration}",
            "provisioningState": "Succeeded",
        }}
        parameters = []

        def az(*args):
            if args[:2] == ("provider", "show"):
                return {"registrationState": "Registered"}
            if args[0] == "rest":
                self.assertEqual(args[-2:], ("--subscription", REMOTE))
                url = args[args.index("--url") + 1]
                if "registrationDefinitions?" in url:
                    self.assertEqual(url, f"{lab.ARM}/subscriptions/{REMOTE}/providers/"
                                     f"Microsoft.ManagedServices/registrationDefinitions?api-version={lab.MANAGED_API}")
                    return {"value": [definition] if legacy or existing else []}
                self.assertEqual(url, f"{lab.ARM}{scope}/providers/Microsoft.ManagedServices/"
                                 f"registrationAssignments?api-version={lab.MANAGED_API}")
                return {"value": [assignment] if existing else []}
            if args[:2] == ("group", "show"):
                self.assertEqual(args[args.index("--name") + 1], "network")
                self.assertEqual(args[-2:], ("--query", "location"))
                return "westeurope"
            if args[:2] == ("deployment", "sub"):
                self.assertEqual("--no-pretty-print" in args, args[2] == "what-if")
                path = Path(args[args.index("--parameters") + 1][1:])
                parameters.append(json.loads(path.read_text())["parameters"])
                return {}
            self.fail(f"Unexpected Azure CLI call: {args}")

        self.mock("current_account")
        self.mock("check_resolvers")
        self.mock("live_group", side_effect=lab.group_scope)
        self.mock("permissions")
        self.mock("confirm")
        az_mock = self.mock("az", side_effect=az)
        return az_mock, parameters, authorization

    def test_lighthouse_delegates_only_uami_and_deduplicates_resource_groups(self):
        az, parameters, authorization = self.delegation_calls()
        lab.delegations(self.args, [TARGET, {**TARGET, "resolverVnetId": VNET + "-second"}], state())
        self.assertEqual(len(parameters), 1)
        self.assertEqual(parameters[0]["authorizations"]["value"], [authorization])
        self.assertEqual(parameters[0]["managedByTenantId"]["value"], TENANT)
        self.assertEqual(parameters[0]["resolverResourceGroup"]["value"], "network")
        deployment = next(call.args for call in az.call_args_list if call.args[:2] == ("deployment", "sub"))
        self.assertEqual(deployment[deployment.index("--subscription") + 1], REMOTE)
        self.assertEqual(deployment[deployment.index("--location") + 1], "westeurope")
        self.assertNotIn("--resource-group", deployment)
        self.assertNotIn("--mode", deployment)
        lab.confirm.assert_called_once_with("delegate", OTHER)

    def test_existing_resource_group_assignment_references_subscription_definition(self):
        _, parameters, _ = self.delegation_calls(existing=True)
        lab.delegations(self.args, [TARGET], state())
        self.assertEqual(parameters, [])
        lab.confirm.assert_not_called()

    def test_rejects_assignment_referencing_definition_at_wrong_scope(self):
        _, parameters, _ = self.delegation_calls(existing=True, wrong_definition_scope=True)
        with self.assertRaisesRegex(ValueError, "Conflicting existing Lighthouse assignment"):
            lab.delegations(self.args, [TARGET], state())
        self.assertEqual(parameters, [])
        lab.confirm.assert_not_called()

    def test_delegation_previews_use_subscription_deployment_without_confirmation(self):
        for action in ("what-if", "validate"):
            with self.subTest(action=action):
                az, parameters, _ = self.delegation_calls()
                self.args.action = action
                lab.delegations(self.args, [TARGET], state())
                self.assertEqual(len(parameters), 1)
                self.assertEqual(az.call_args.args[:3], ("deployment", "sub", action))
                lab.confirm.assert_not_called()

    def test_legacy_delegation_fails_without_overwrite(self):
        _, parameters, _ = self.delegation_calls(legacy=True)
        with self.assertRaisesRegex(ValueError, "legacy dual-identity delegation"):
            lab.delegations(self.args, [TARGET], state())
        self.assertEqual(parameters, [])
        lab.confirm.assert_not_called()

    def test_link_workflow_supports_both_callers_and_preserves_existing_links(self):
        for user_type, object_id in (("user", USER), ("servicePrincipal", OBJECT)):
            with self.subTest(user_type=user_type):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(lab.central, "load_zone_names",
                                                    return_value=([], {"private.example"})))
                    for name in ("live_subscription", "check_resolvers", "permissions", "confirm"):
                        stack.enter_context(patch.object(lab, name))
                    stack.enter_context(patch.object(lab, "live_group", side_effect=lab.group_scope))
                    existing = {"name": "keep-existing", "virtualNetwork": {"id": VNET},
                                "provisioningState": "Succeeded", "registrationEnabled": True}
                    responses = [
                        {"tenantId": TENANT, "user": {"type": user_type, "name": "userAssignedIdentity"}},
                        token(object_id=object_id),
                        {"id": lab.group_scope(SUBSCRIPTION, GROUP) + "/providers/Microsoft.Network/privateDnsZones/private.example"},
                        [existing],
                    ]
                    az = stack.enter_context(patch.object(lab, "az", side_effect=responses))
                    lab.link_zones(self.args, [TARGET], state(), DOCUMENTS)
                    self.assertEqual(az.call_count, 4)
                    lab.confirm.assert_not_called()
                    lab.permissions.assert_any_call(
                        VNET, ["Microsoft.Network/virtualNetworks/read", "Microsoft.Network/virtualNetworks/join/action"],
                        subscription=REMOTE,
                    )

    def test_creates_only_missing_links_with_either_supported_caller(self):
        for user_type, object_id in (("user", USER), ("servicePrincipal", OBJECT)):
            with self.subTest(user_type=user_type):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(lab.central, "load_zone_names",
                                                    return_value=([], {"private.example"})))
                    for name in ("live_subscription", "check_resolvers", "permissions", "confirm"):
                        stack.enter_context(patch.object(lab, name))
                    stack.enter_context(patch.object(lab, "live_group", side_effect=lab.group_scope))
                    replacements = stack.enter_context(patch.object(lab, "noncentral_link_replacements",
                                                                   return_value=[]))
                    existing = {"name": "central-resolver", "virtualNetwork": {"id": VNET.replace(REMOTE, SUBSCRIPTION)},
                                "provisioningState": "Succeeded", "registrationEnabled": True}
                    responses = [
                        {"tenantId": TENANT, "user": {"type": user_type, "name": "userAssignedIdentity"}},
                        token(object_id=object_id),
                        {"id": lab.group_scope(SUBSCRIPTION, GROUP) + "/providers/Microsoft.Network/privateDnsZones/private.example"},
                        [existing],
                        {"virtualNetwork": {"id": VNET}, "provisioningState": "Succeeded"},
                    ]
                    az = stack.enter_context(patch.object(lab, "az", side_effect=responses))
                    lab.link_zones(self.args, [TARGET], state(), DOCUMENTS)
                    replacements.assert_called_once()
                    writes = [call.args for call in az.call_args_list if "create" in call.args]
                    self.assertEqual(len(writes), 1)
                    create = writes[0]
                    self.assertEqual(create[:5], ("network", "private-dns", "link", "vnet", "create"))
                    self.assertEqual(create[create.index("--subscription") + 1], SUBSCRIPTION)
                    self.assertEqual(create[create.index("--virtual-network") + 1], VNET)
                    self.assertEqual(create[create.index("--registration-enabled") + 1], "false")
                    lab.confirm.assert_called_once_with("links", f"{TENANT}/{SUBSCRIPTION}/{GROUP}")

    def replacement_calls(self, delete_failure=False, create_failure=False, deletion_incomplete=False):
        local_subscription = "66666666-6666-6666-6666-666666666666"
        local_zone_id = (f"/subscriptions/{local_subscription}/resourceGroups/local-dns"
                         "/providers/Microsoft.Network/privateDnsZones/Private.Example")
        local = {
            "id": local_zone_id + "/virtualNetworkLinks/local-resolver",
            "name": "local-resolver", "virtualNetwork": {"id": VNET.upper()},
            "provisioningState": "Succeeded", "registrationEnabled": True, "etag": '"original"',
        }
        unrelated = {**local, "id": local_zone_id + "/virtualNetworkLinks/other-vnet",
                     "name": "other-vnet", "virtualNetwork": {"id": VNET + "-other"}}
        remaining = [local, unrelated]
        calls = []
        documents = {OTHER: {"subscriptions": [{"id": REMOTE}, {"id": local_subscription}]}}

        def az(*args):
            calls.append(args)
            if args[:4] == ("network", "private-dns", "zone", "show"):
                return {"id": lab.group_scope(SUBSCRIPTION, GROUP)
                        + "/providers/Microsoft.Network/privateDnsZones/private.example"}
            if args[:5] == ("network", "private-dns", "link", "vnet", "list"):
                subscription = args[args.index("--subscription") + 1]
                if subscription == SUBSCRIPTION:
                    return []
                self.assertEqual(subscription, local_subscription)
                self.assertEqual(args[args.index("--resource-group") + 1], "local-dns")
                self.assertEqual(args[args.index("--zone-name") + 1], "Private.Example")
                return list(remaining)
            if args[0] == "rest":
                subscription = args[args.index("--subscription") + 1]
                self.assertIn(f"/subscriptions/{subscription}/providers/Microsoft.Network/privateDnsZones?",
                              args[args.index("--url") + 1])
                return {"value": [
                    {"name": "Private.Example", "id": local_zone_id},
                    {"name": "unselected.example", "id": local_zone_id.replace("Private.Example", "unselected.example")},
                ] if subscription == local_subscription else []}
            if args[:5] == ("network", "private-dns", "link", "vnet", "delete"):
                self.assertEqual(args[args.index("--name") + 1], "local-resolver")
                self.assertEqual(args[args.index("--subscription") + 1], local_subscription)
                self.assertEqual(args[args.index("--if-match") + 1], '"original"')
                self.assertIn("--yes", args)
                if delete_failure:
                    raise ValueError("Azure CLI delete failed: PreconditionFailed")
                if not deletion_incomplete:
                    remaining.remove(local)
                return None
            if args[:5] == ("network", "private-dns", "link", "vnet", "create"):
                self.assertEqual(args[args.index("--subscription") + 1], SUBSCRIPTION)
                self.assertEqual(args[args.index("--virtual-network") + 1], VNET)
                if create_failure:
                    raise ValueError("Azure CLI central link creation failed")
                return {"virtualNetwork": {"id": VNET}, "provisioningState": "Succeeded"}
            self.fail(f"Unexpected Azure CLI call: {args}")

        self.stack.enter_context(patch.object(lab.central, "load_zone_names",
                                             return_value=([], {"private.example"})))
        for name in ("current_account", "live_subscription", "check_resolvers", "permissions", "confirm"):
            self.mock(name)
        self.mock("live_group", side_effect=lab.group_scope)
        self.mock("az", side_effect=az)
        return calls, local, remaining, documents

    def test_replaces_only_same_name_local_resolver_link_before_central_creation(self):
        calls, local, remaining, documents = self.replacement_calls()
        lab.link_zones(self.args, [TARGET], state(), documents)
        writes = [args for args in calls if args[:5] in (
            ("network", "private-dns", "link", "vnet", "delete"),
            ("network", "private-dns", "link", "vnet", "create"),
        )]
        self.assertEqual([args[4] for args in writes], ["delete", "create"])
        self.assertEqual([link["name"] for link in remaining], ["other-vnet"])
        lab.permissions.assert_any_call(
            local["id"], ["Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
                          "Microsoft.Network/privateDnsZones/virtualNetworkLinks/delete"],
            subscription="66666666-6666-6666-6666-666666666666",
        )
        lab.confirm.assert_called_once()

    def test_replacement_preview_reports_removal_without_writes(self):
        for action in ("validate", "what-if"):
            with self.subTest(action=action):
                calls, _, remaining, documents = self.replacement_calls()
                self.args.action = action
                with redirect_stdout(io.StringIO()) as output:
                    lab.link_zones(self.args, [TARGET], state(), documents)
                self.assertIn("1 to create; 1 non-central links to delete", output.getvalue())
                self.assertIn("auto-registered records", output.getvalue())
                self.assertFalse(any("delete" in args or "create" in args for args in calls))
                self.assertEqual(len(remaining), 2)
                lab.confirm.assert_not_called()

    def test_permission_or_confirmation_failure_preserves_local_links(self):
        for failure in ("permission", "confirmation"):
            with self.subTest(failure=failure):
                calls, _, remaining, documents = self.replacement_calls()
                if failure == "permission":
                    def permissions(scope, *args, **kwargs):
                        if scope.endswith("/virtualNetworkLinks/local-resolver"):
                            raise ValueError("Missing delete permission")
                    lab.permissions.side_effect = permissions
                else:
                    lab.confirm.side_effect = ValueError("Cancelled")
                with self.assertRaises(ValueError):
                    lab.link_zones(self.args, [TARGET], state(), documents)
                self.assertFalse(any("delete" in args or "create" in args for args in calls))
                self.assertEqual(len(remaining), 2)

    def test_changed_or_unhealthy_local_links_are_not_deleted(self):
        for mutation in ({"etag": None}, {"provisioningState": "Updating"}):
            with self.subTest(mutation=mutation):
                calls, local, remaining, documents = self.replacement_calls()
                local.update(mutation)
                with self.assertRaises(ValueError):
                    lab.link_zones(self.args, [TARGET], state(), documents)
                self.assertFalse(any("delete" in args or "create" in args for args in calls))
                self.assertEqual(len(remaining), 2)

    def test_conditional_delete_failure_stops_before_central_creation(self):
        calls, _, remaining, documents = self.replacement_calls(delete_failure=True)
        with self.assertRaisesRegex(ValueError, "PreconditionFailed"):
            lab.link_zones(self.args, [TARGET], state(), documents)
        self.assertFalse(any("create" in args for args in calls))
        self.assertEqual(len(remaining), 2)

    def test_incomplete_deletion_stops_before_central_creation(self):
        calls, _, _, documents = self.replacement_calls(deletion_incomplete=True)
        with self.assertRaisesRegex(ValueError, "deletion verification failed"):
            lab.link_zones(self.args, [TARGET], state(), documents)
        self.assertFalse(any("create" in args for args in calls))

    def test_central_creation_failure_reports_previous_verified_removal(self):
        _, _, remaining, documents = self.replacement_calls(create_failure=True)
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError, "central link creation failed"):
                lab.link_zones(self.args, [TARGET], state(), documents)
        self.assertIn("Removed and verified non-central link", output.getvalue())
        self.assertEqual([link["name"] for link in remaining], ["other-vnet"])

    def test_local_links_to_other_vnets_are_preserved(self):
        calls, local, remaining, documents = self.replacement_calls()
        local["virtualNetwork"] = {"id": VNET + "-unmapped"}
        lab.link_zones(self.args, [TARGET], state(), documents)
        self.assertFalse(any("delete" in args for args in calls))
        self.assertEqual(len(remaining), 2)
        self.assertEqual(sum("create" in args for args in calls), 1)

    def test_rejects_zone_outside_its_noncentral_subscription(self):
        _, _, _, documents = self.replacement_calls()
        self.mock("pages", return_value=[{
            "name": "private.example",
            "id": lab.group_scope(SUBSCRIPTION, GROUP)
                  + "/providers/Microsoft.Network/privateDnsZones/private.example",
        }])
        with self.assertRaisesRegex(ValueError, "unexpected ID"):
            lab.noncentral_link_replacements([{"zone": "private.example", **TARGET}], documents)
        lab.confirm.assert_not_called()

    def test_replacements_can_be_found_in_another_inventoried_noncentral_tenant(self):
        calls, local, _, _ = self.replacement_calls()
        third_tenant = "77777777-7777-7777-7777-777777777777"
        documents = {
            OTHER: {"subscriptions": [{"id": REMOTE}]},
            third_tenant: {"subscriptions": [{"id": "66666666-6666-6666-6666-666666666666"}]},
        }
        replacements = lab.noncentral_link_replacements([{"zone": "private.example", **TARGET}], documents)
        self.assertEqual([link["id"] for link in replacements], [local["id"]])
        lab.live_subscription.assert_any_call("66666666-6666-6666-6666-666666666666", third_tenant)
        self.assertFalse(any("delete" in args for args in calls))

    def test_header_only_csv_has_no_link_reads_or_deletions(self):
        self.stack.enter_context(patch.object(lab.central, "load_zone_names", return_value=([], set())))
        az = self.mock("az")
        lab.link_zones(self.args, [TARGET], state(), DOCUMENTS)
        az.assert_not_called()


if __name__ == "__main__":
    unittest.main()

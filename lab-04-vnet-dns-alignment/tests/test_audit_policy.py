from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("lab04_policy", ROOT / "scripts/audit_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
finally:
    sys.path.pop(0)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUB = "11111111-1111-1111-1111-111111111111"
SECOND_SUB = "22222222-2222-2222-2222-222222222222"
OTHER_SUB = "33333333-3333-3333-3333-333333333333"
RESOLVER = f"/subscriptions/{SUB}/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver"
ENDPOINT = f"/subscriptions/{SUB}/resourceGroups/network/providers/Microsoft.Network/dnsResolvers/resolver/inboundEndpoints/inbound"
IP = "10.10.1.4"


def evaluate_rule(rule, resource_id, resource_type, dns, parameters):
    fields = {"type": resource_type, "id": resource_id, policy.DNS_ALIAS: dns or []}
    for condition in rule["if"]["allOf"]:
        if "field" in condition:
            actual = fields[condition["field"]]
            operator = "equals" if "equals" in condition else "notEquals"
            expected = condition[operator]
            parameter = re.fullmatch(r"\[parameters\('([^']+)'\)\]", expected)
            if parameter:
                expected = parameters[parameter[1]]["value"]
            equal = actual.casefold() == expected.casefold()
            if equal != (operator == "equals"):
                return False
        else:
            expression = re.fullmatch(
                r"\[equals\(field\('([^']+)'\), parameters\('([^']+)'\)\)\]", condition["value"],
            )
            if expression is None:
                raise AssertionError(f"Unsupported policy expression: {condition}")
            equal = fields[expression[1]] == parameters[expression[2]]["value"]
            if equal != condition["equals"]:
                return False
    return rule["then"]["effect"] == "audit"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stdout = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stderr = self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.map = self.directory / "resolvers.json"
        self.map.write_text(json.dumps({"schemaVersion": 1, "resolvers": [{
            "tenantId": TENANT, "subscriptionId": SUB, "resolverVnetId": RESOLVER,
        }]}))
        self.documents = {TENANT: {
            "virtualNetworks": [{"id": RESOLVER}],
            "subscriptions": [{"id": SUB}, {"id": SECOND_SUB}],
        }}
        self.resources = {}
        self.puts = []
        self.fail_id = None
        self.corrupt_put = False
        self.ip = IP
        self.mock_common("discover", return_value=self.documents)
        self.accounts = self.mock_common("current_account")
        self.mock_common("live_subscription")
        self.permissions = self.mock_common("permissions")
        self.confirm = self.mock_common("confirm")
        self.mock_common("pages", side_effect=self.pages)
        self.az = self.mock_common("az", side_effect=self.azure)
        self.stack.enter_context(patch.object(policy.alignment, "read_vnet", return_value=([], '"etag"')))
        self.stack.enter_context(patch.object(policy.alignment, "inbound_endpoint", side_effect=self.endpoint))

    def mock_common(self, name, **kwargs):
        return self.stack.enter_context(patch.object(policy.common, name, **kwargs))

    def endpoint(self, target, options):
        subscription = target["subscriptionId"]
        return {"id": ENDPOINT.replace(SUB, subscription),
                "dns": [self.ip if subscription == SUB else "10.30.1.4"]}

    def pages(self, url, subscription=None):
        prefix = url.removeprefix(policy.common.ARM).split("?")[0] + "/"
        return [deepcopy(value) for key, value in self.resources.items() if key.startswith(prefix)]

    def azure(self, *args):
        if args[:2] == ("provider", "show"):
            return {"resourceTypes": [{"resourceType": "virtualNetworks", "aliases": [{"name": policy.DNS_ALIAS}]}]}
        self.assertEqual(args[0], "rest")
        method = args[args.index("--method") + 1]
        resource_id = args[args.index("--url") + 1].removeprefix(policy.common.ARM).split("?")[0]
        if "/inboundEndpoints/" in resource_id:
            subscription = args[args.index("--subscription") + 1]
            return {"id": resource_id, "properties": {
                "provisioningState": "Succeeded", "ipConfigurations": [{
                    "privateIpAddress": self.ip if subscription == SUB else "10.30.1.4",
                    "subnet": {"id": RESOLVER.replace(SUB, subscription) + "/subnets/inbound"},
                }],
            }}
        if method == "put":
            body = json.loads(args[args.index("--body") + 1])
            self.puts.append((resource_id, body))
            if resource_id == self.fail_id:
                raise ValueError("Policy write denied")
            self.resources[resource_id] = {"id": resource_id, **deepcopy(body)}
            if self.corrupt_put:
                self.resources[resource_id]["properties"]["mode"] = "Indexed"
            return deepcopy(self.resources[resource_id])
        self.assertEqual(method, "get")
        return deepcopy(self.resources[resource_id])

    def run_main(self, action, *extra):
        return policy.main([action, "--inventory-directory", str(self.directory),
                            "--resolver-map", str(self.map), *extra])

    def test_rule_audits_exact_ordered_dns_and_all_vnets_except_resolver(self):
        parameters = {"resolverVnetId": {"value": RESOLVER}, "dnsServers": {"value": [IP, "10.10.1.5"]}}
        rule = policy.definition()["properties"]["policyRule"]
        unlisted_vnet = RESOLVER.replace("virtualNetworks/resolver", "virtualNetworks/not-in-inventory")
        for dns, expected in (
            ([IP, "10.10.1.5"], False),
            (None, True), ([], True), (["8.8.8.8"], True), ([IP], True),
            (["10.10.1.5", IP], True), ([IP, IP], True), ([IP, "10.10.1.5", "8.8.8.8"], True),
        ):
            with self.subTest(dns=dns):
                self.assertEqual(evaluate_rule(
                    rule, unlisted_vnet, "Microsoft.Network/virtualNetworks", dns, parameters,
                ), expected)
        self.assertFalse(evaluate_rule(rule, RESOLVER.upper(), "Microsoft.Network/virtualNetworks", [], parameters))
        self.assertFalse(evaluate_rule(rule, unlisted_vnet, "Microsoft.Compute/virtualMachines", [], parameters))

    def test_create_covers_all_inventory_subscriptions_and_rerun_is_noop(self):
        self.assertEqual(self.run_main("create"), 0)
        self.assertEqual(len(self.puts), 4)
        for subscription in (SUB, SECOND_SUB):
            self.accounts.assert_any_call(TENANT, subscription, human=True)
            assignment_id = f"/subscriptions/{subscription}/providers/Microsoft.Authorization/policyAssignments/{policy.NAME}"
            properties = self.resources[assignment_id]["properties"]
            self.assertEqual(properties["parameters"]["dnsServers"]["value"], [IP])
            self.assertEqual(properties["parameters"]["resolverVnetId"]["value"], RESOLVER)
            self.assertEqual(properties["enforcementMode"], "DoNotEnforce")
            self.assertNotIn("identity", self.resources[assignment_id])
        self.confirm.assert_called_once_with("dns-policy", TENANT)
        self.puts.clear()
        self.confirm.reset_mock()
        self.assertEqual(self.run_main("create"), 0)
        self.assertEqual(self.puts, [])
        self.confirm.assert_not_called()

    def test_dry_runs_and_cancel_never_put_policy_resources(self):
        for action in ("validate", "what-if"):
            self.assertEqual(self.run_main(action), 0)
        self.confirm.assert_not_called()
        self.confirm.side_effect = ValueError("Cancelled")
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.puts, [])

    def test_all_scope_preflight_failure_prevents_any_policy_creation(self):
        def check(scope, actions, subscription=None):
            if subscription == SECOND_SUB:
                raise ValueError("Missing policy write permission")
        self.permissions.side_effect = check
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.puts, [])
        self.confirm.assert_not_called()

    def test_missing_dns_alias_prevents_deploying_an_invalid_policy(self):
        self.az.side_effect = lambda *args: {"resourceTypes": [{"resourceType": "virtualNetworks", "aliases": []}]}
        self.assertEqual(self.run_main("create"), 1)
        self.assertIn("alias is unavailable", self.stderr.getvalue())
        self.assertEqual(self.puts, [])
        self.confirm.assert_not_called()

    def test_endpoint_and_policy_changes_after_preview_stop_before_writes(self):
        self.confirm.side_effect = lambda *args: setattr(self, "ip", "10.10.1.5")
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.puts, [])
        self.assertIn("Inbound endpoint changed after preview", self.stderr.getvalue())
        resource_id = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyDefinitions/{policy.NAME}"
        def competing_change(*args):
            self.resources[resource_id] = {"id": resource_id, **policy.definition()}
        self.confirm.side_effect = competing_change
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.puts, [])
        self.assertIn("Policy changed after preview", self.stderr.getvalue())

    def test_existing_conflicting_definition_is_not_overwritten(self):
        resource_id = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyDefinitions/{policy.NAME}"
        body = policy.definition()
        body["properties"]["policyRule"]["then"]["effect"] = "deny"
        self.resources[resource_id] = {"id": resource_id, **body}
        before = deepcopy(self.resources)
        self.assertEqual(self.run_main("create", "--update-existing"), 1)
        self.assertEqual(self.resources, before)
        self.assertEqual(self.puts, [])

    def test_changed_dns_requires_explicit_assignment_refresh(self):
        self.assertEqual(self.run_main("create"), 0)
        self.puts.clear()
        self.ip = "10.10.1.5"
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.puts, [])
        self.assertEqual(self.run_main("create", "--update-existing"), 0)
        self.assertEqual(len(self.puts), 2)
        for resource_id, body in self.puts:
            self.assertIn("/policyAssignments/", resource_id)
            self.assertEqual(body["properties"]["parameters"]["dnsServers"]["value"], [self.ip])

    def test_foreign_assignment_and_scope_filters_block_refresh(self):
        self.assertEqual(self.run_main("create"), 0)
        self.puts.clear()
        resource_id = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyAssignments/{policy.NAME}"
        original = deepcopy(self.resources[resource_id])
        for change in (
            {"metadata": {"lab": "another-lab"}},
            {"notScopes": ["/subscriptions/" + SUB + "/resourceGroups/skip"]},
            {"overrides": [{"kind": "policyEffect", "value": "deny"}]},
        ):
            with self.subTest(change=change):
                self.resources[resource_id] = deepcopy(original)
                self.resources[resource_id]["properties"].update(change)
                self.assertEqual(self.run_main("create", "--update-existing"), 1)
                self.assertEqual(self.puts, [])

    def test_write_or_verification_failure_stops_before_later_subscriptions(self):
        self.fail_id = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyAssignments/{policy.NAME}"
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(len(self.puts), 2)
        self.assertTrue(all(SECOND_SUB not in resource_id for resource_id, _ in self.puts))
        self.assertIn("not rolled back", self.stderr.getvalue())
        self.resources.clear()
        self.puts.clear()
        self.fail_id = None
        self.corrupt_put = True
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(len(self.puts), 1)

    def test_each_tenant_gets_its_own_resolver_ips(self):
        other_resolver = RESOLVER.replace(SUB, OTHER_SUB)
        self.documents[OTHER_TENANT] = {
            "virtualNetworks": [{"id": other_resolver}], "subscriptions": [{"id": OTHER_SUB}],
        }
        mapping = json.loads(self.map.read_text())
        mapping["resolvers"].append({
            "tenantId": OTHER_TENANT, "subscriptionId": OTHER_SUB, "resolverVnetId": other_resolver,
        })
        self.map.write_text(json.dumps(mapping))
        self.assertEqual(self.run_main("create"), 0)
        assignment_id = f"/subscriptions/{OTHER_SUB}/providers/Microsoft.Authorization/policyAssignments/{policy.NAME}"
        parameters = self.resources[assignment_id]["properties"]["parameters"]
        self.assertEqual(parameters["dnsServers"]["value"], ["10.30.1.4"])
        self.assertEqual(parameters["resolverVnetId"]["value"], other_resolver)
        self.accounts.assert_any_call(OTHER_TENANT, OTHER_SUB, human=True)
        self.resources.clear()
        self.puts.clear()
        self.accounts.reset_mock()
        self.assertEqual(self.run_main("create", "--tenant-id", OTHER_TENANT), 0)
        self.assertEqual(len(self.puts), 2)
        self.assertTrue(all(OTHER_SUB in resource_id for resource_id, _ in self.puts))
        self.accounts.assert_called_once_with(OTHER_TENANT, OTHER_SUB, human=True)


if __name__ == "__main__":
    unittest.main()

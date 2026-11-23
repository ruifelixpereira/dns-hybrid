from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lab03_forwarding", ROOT / "scripts/forwarding.py")
forwarding = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    spec.loader.exec_module(forwarding)

CENTRAL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUB = "22222222-2222-2222-2222-222222222222"
GROUP = f"/subscriptions/{SUB}/resourceGroups/network"
VNET = GROUP + "/providers/Microsoft.Network/virtualNetworks/resolver"
RESOLVER = GROUP + "/providers/Microsoft.Network/dnsResolvers/resolver"
ENDPOINT = RESOLVER + "/outboundEndpoints/outbound"
RULESET = GROUP + "/providers/Microsoft.Network/dnsForwardingRulesets/existing"
TARGET = {"tenantId": TENANT, "subscriptionId": SUB, "resourceGroup": "network", "resolverVnetId": VNET}
SERVERS = [{"ipAddress": "10.50.0.10", "port": 53}, {"ipAddress": "10.50.0.11", "port": 5353}]
RULES = {"corp.example.": {"servers": SERVERS, "state": "Enabled"}}


def resource(resource_id, **properties):
    return {"id": resource_id, "name": resource_id.rsplit("/", 1)[-1], "location": "westeurope",
            "properties": {"provisioningState": "Succeeded", **properties}}


def rule(domain="corp.example.", servers=None, name="existing-rule", state="Enabled"):
    return resource(RULESET + "/forwardingRules/" + name, domainName=domain,
                    targetDnsServers=deepcopy(SERVERS if servers is None else servers), forwardingRuleState=state)


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.rules_path = directory / "rules.json"
        self.map_path = directory / "resolvers.json"
        self.map_path.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET]}))
        self.write_rules()

    def mock(self, name, **kwargs):
        return self.stack.enter_context(patch.object(forwarding, name, **kwargs))

    def lab_mock(self, name, **kwargs):
        return self.stack.enter_context(patch.object(forwarding.lab, name, **kwargs))

    def write_rules(self, values=None):
        self.rules_path.write_text(json.dumps({
            "schemaVersion": 1, "rules": [{"domainName": "CORP.EXAMPLE", "targetDnsServers": SERVERS}]
            if values is None else values,
        }))

    def fixtures(self, existing=False, linked=False, rules=None, multiple_endpoints=False):
        self.endpoints = [resource(ENDPOINT)]
        if multiple_endpoints:
            self.endpoints.append(resource(ENDPOINT + "-second"))
        self.rulesets = [resource(RULESET, dnsResolverOutboundEndpoints=[{"id": ENDPOINT}])] if existing else []
        self.links = [resource(RULESET + "/virtualNetworkLinks/keep-name", virtualNetwork={"id": VNET})] if linked else []
        self.rules = [] if rules is None else rules

        def listing(resource_id, child, subscription):
            self.assertEqual(subscription, SUB)
            if child == "providers/Microsoft.Network/dnsResolvers":
                self.assertEqual(resource_id, GROUP)
                return [resource(RESOLVER, virtualNetwork={"id": VNET})]
            if child == "outboundEndpoints":
                self.assertEqual(resource_id, RESOLVER)
                return self.endpoints
            if child == "inboundEndpoints":
                return [resource(RESOLVER + "/inboundEndpoints/inbound",
                                 ipConfigurations=[{"privateIpAddress": "10.2.0.4"}])]
            if child == "providers/Microsoft.Network/dnsForwardingRulesets":
                return self.rulesets
            if child == "virtualNetworkLinks":
                return self.links
            if child == "forwardingRules":
                return self.rules
            self.fail(f"Unexpected list: {resource_id}/{child}")

        self.mock("listing", side_effect=listing)
        self.lab_mock("permissions")

    def test_normalizes_domains_and_preserves_dns_server_priority_and_ports(self):
        self.assertEqual(forwarding.load_rules(self.rules_path), RULES)
        self.assertEqual(forwarding.domain_name("_msdcs.CORP.EXAMPLE."), "_msdcs.corp.example.")
        self.write_rules([{"domainName": "corp", "targetDnsServers": [{"ipAddress": "10.0.0.10"}]}])
        self.assertEqual(forwarding.load_rules(self.rules_path), {
            "corp.": {"servers": [{"ipAddress": "10.0.0.10", "port": 53}], "state": "Enabled"},
        })

    def test_rejects_bad_domains_targets_and_duplicate_domains_before_azure(self):
        for domain in ("*.example", "a..example", "example..", "a" * 64 + ".example", "../example"):
            with self.subTest(domain=domain):
                with self.assertRaises(ValueError):
                    forwarding.domain_name(domain)
        for values in ([], [{"ipAddress": "168.63.129.16"}], [{"ipAddress": "127.0.0.1"}],
                       [{"ipAddress": "invalid"}], [{"ipAddress": "10.0.0.1", "port": True}],
                       [{"ipAddress": "10.0.0.1", "port": 65536}], [SERVERS[0], SERVERS[0]]):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    forwarding.servers(values)
        self.write_rules([
            {"domainName": "corp.example", "targetDnsServers": SERVERS},
            {"domainName": "CORP.EXAMPLE.", "targetDnsServers": SERVERS},
        ])
        azure = self.lab_mock("az")
        self.assertEqual(self.run_main("create"), 1)
        azure.assert_not_called()

    def test_new_ruleset_plan_links_exact_resolver_vnet_and_outbound(self):
        self.fixtures()
        plan = forwarding.plan_target(TARGET, {}, RULES)
        self.assertTrue(plan["create"])
        self.assertTrue(plan["link"])
        self.assertEqual(plan["endpoint"]["id"], ENDPOINT)
        self.assertEqual(plan["vnet"], VNET)
        self.assertEqual(plan["rules"][0]["servers"], SERVERS)
        self.assertEqual(plan["group"], "network")

    def test_reuses_existing_ruleset_and_link_and_adds_only_missing_domains(self):
        self.fixtures(existing=True, linked=True, rules=[rule()])
        desired = {**RULES, "branch.example.": {"servers": [SERVERS[0]], "state": "Enabled"}}
        plan = forwarding.plan_target(TARGET, {}, desired)
        self.assertFalse(plan["create"])
        self.assertFalse(plan["link"])
        self.assertEqual(plan["id"], RULESET)
        self.assertEqual([value["domain"] for value in plan["rules"]], ["branch.example."])
        self.assertEqual(self.rules[0]["name"], "existing-rule")

    def test_existing_endpoint_ruleset_is_linked_instead_of_duplicated(self):
        self.fixtures(existing=True, rules=[rule()])
        plan = forwarding.plan_target(TARGET, {}, RULES)
        self.assertFalse(plan["create"])
        self.assertTrue(plan["link"])
        self.assertEqual(plan["rules"], [])

    def test_identical_ruleset_is_a_complete_noop(self):
        self.fixtures(existing=True, linked=True, rules=[rule()])
        plan = forwarding.plan_target(TARGET, {}, RULES)
        self.assertFalse(plan["create"] or plan["link"] or plan["rules"])
        apply = self.mock("apply")
        self.assertEqual(self.run_main("create"), 0)
        apply.assert_not_called()
        forwarding.lab.confirm.assert_not_called()

    def test_conflicting_rules_are_not_overwritten(self):
        for existing in (rule(state="Disabled"), rule(servers=list(reversed(SERVERS))),
                         rule(servers=[{"ipAddress": "10.50.0.20", "port": 53}])):
            with self.subTest(existing=existing):
                self.fixtures(existing=True, linked=True, rules=[existing])
                with self.assertRaisesRegex(ValueError, "Conflicting forwarding rule"):
                    forwarding.plan_target(TARGET, {}, RULES)

    def test_ambiguous_endpoint_requires_explicit_selection(self):
        self.fixtures(multiple_endpoints=True)
        with self.assertRaisesRegex(ValueError, "outboundEndpointId"):
            forwarding.plan_target(TARGET, {}, RULES)
        plan = forwarding.plan_target(TARGET, {"outboundEndpointId": ENDPOINT}, RULES)
        self.assertEqual(plan["endpoint"]["id"], ENDPOINT)
        with self.assertRaises(ValueError):
            forwarding.plan_target(TARGET, {"outboundEndpointId": ENDPOINT.replace(SUB, CENTRAL)}, RULES)

    def test_ambiguous_ruleset_requires_explicit_selection(self):
        self.fixtures(existing=True, linked=True)
        self.rulesets.append(resource(RULESET + "-second", dnsResolverOutboundEndpoints=[{"id": ENDPOINT}]))
        with self.assertRaisesRegex(ValueError, "forwardingRulesetId"):
            forwarding.plan_target(TARGET, {}, RULES)
        plan = forwarding.plan_target(TARGET, {"forwardingRulesetId": RULESET}, RULES)
        self.assertEqual(plan["id"], RULESET)

    def test_inbound_loop_and_wrong_outbound_fail_preflight(self):
        self.fixtures(existing=True, linked=True)
        with self.assertRaisesRegex(ValueError, "DNS loop"):
            forwarding.plan_target(TARGET, {}, {
                "corp.example.": {"servers": [{"ipAddress": "10.2.0.4", "port": 53}], "state": "Enabled"},
            })
        self.rules = [rule(domain="other.example.", servers=[{"ipAddress": "10.2.0.4", "port": 53}])]
        with self.assertRaisesRegex(ValueError, "inbound endpoint"):
            forwarding.plan_target(TARGET, {}, RULES)
        self.rules = []
        self.rulesets[0]["properties"]["dnsResolverOutboundEndpoints"] = [{"id": ENDPOINT + "-wrong"}]
        with self.assertRaisesRegex(ValueError, "different outbound endpoint"):
            forwarding.plan_target(TARGET, {}, RULES)

    def test_rule_limit_is_checked_before_changes(self):
        self.fixtures(existing=True, linked=True, rules=[
            rule(domain=f"domain{index}.example.", name=f"rule{index}") for index in range(1000)
        ])
        with self.assertRaisesRegex(ValueError, "rule limit"):
            forwarding.plan_target(TARGET, {}, RULES)

    def apply_calls(self, plan, invalid_rule=False):
        created = {} if plan["create"] else {
            plan["id"]: resource(plan["id"], dnsResolverOutboundEndpoints=[{"id": ENDPOINT}]),
        }
        writes = []

        def az(*args):
            if args[0] == "rest":
                resource_id = args[args.index("--url") + 1].split("?")[0].removeprefix(forwarding.lab.ARM)
                return created[resource_id]
            self.assertEqual(args[0], "dns-resolver")
            self.assertEqual(args[args.index("--if-none-match") + 1], "*")
            self.assertEqual(args[args.index("--subscription") + 1], SUB)
            self.assertEqual(args[args.index("--resource-group") + 1], "network")
            writes.append(args)
            if args[1] == "forwarding-ruleset":
                refs = json.loads(args[args.index("--outbound-endpoints") + 1])
                self.assertEqual(refs, [{"id": ENDPOINT}])
                created[plan["id"]] = resource(plan["id"], dnsResolverOutboundEndpoints=refs)
            elif args[1] == "forwarding-rule":
                value = plan["rules"][0]
                self.assertEqual(args[args.index("--forwarding-rule-state") + 1], value["state"])
                dns_servers = json.loads(args[args.index("--target-dns-servers") + 1])
                self.assertEqual(dns_servers, [{"ip-address": item["ipAddress"], "port": item["port"]} for item in SERVERS])
                resource_id = plan["id"] + "/forwardingRules/" + value["name"]
                created[resource_id] = resource(
                    resource_id, domainName=value["domain"], forwardingRuleState=value["state"],
                    targetDnsServers=[] if invalid_rule else SERVERS,
                )
            elif args[1] == "vnet-link":
                self.assertEqual(args[args.index("--id") + 1], VNET)
                resource_id = plan["id"] + "/virtualNetworkLinks/" + plan["linkName"]
                created[resource_id] = resource(resource_id, virtualNetwork={"id": VNET})
            else:
                self.fail(f"Unexpected write: {args}")
            return {}

        self.lab_mock("az", side_effect=az)
        return writes

    def test_create_is_conditional_and_verified_before_linking(self):
        self.fixtures()
        plan = forwarding.plan_target(TARGET, {}, RULES)
        writes = self.apply_calls(plan)
        forwarding.apply(plan)
        self.assertEqual([args[1] for args in writes], ["forwarding-ruleset", "forwarding-rule", "vnet-link"])
        self.assertIn("Created and verified ruleset VNet link", self.output.getvalue())

    def test_main_creates_verified_ruleset_rules_and_link_after_confirmation(self):
        self.fixtures()
        plan = forwarding.plan_target(TARGET, {}, RULES)
        writes = self.apply_calls(plan)
        self.assertEqual(self.run_main("create"), 0)
        forwarding.lab.confirm.assert_called_once_with("forwarding", TENANT)
        self.assertEqual([args[1] for args in writes], ["forwarding-ruleset", "forwarding-rule", "vnet-link"])

    def test_existing_linked_ruleset_only_gets_missing_rule(self):
        self.fixtures(existing=True, linked=True, rules=[rule(domain="other.example.")])
        plan = forwarding.plan_target(TARGET, {}, RULES)
        writes = self.apply_calls(plan)
        forwarding.apply(plan)
        self.assertEqual([args[1] for args in writes], ["forwarding-rule"])

    def test_rule_verification_failure_stops_before_vnet_link_creation(self):
        self.fixtures()
        plan = forwarding.plan_target(TARGET, {}, RULES)
        writes = self.apply_calls(plan, invalid_rule=True)
        with self.assertRaises(ValueError):
            forwarding.apply(plan)
        self.assertEqual([args[1] for args in writes], ["forwarding-ruleset", "forwarding-rule"])

    def test_reused_ruleset_changed_endpoint_stops_before_writes(self):
        self.fixtures(existing=True, linked=True)
        plan = forwarding.plan_target(TARGET, {}, RULES)
        az = self.lab_mock("az", return_value=resource(
            RULESET, dnsResolverOutboundEndpoints=[{"id": ENDPOINT + "-different"}],
        ))
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            forwarding.apply(plan)
        self.assertEqual(az.call_count, 1)
        self.assertEqual(az.call_args.args[:3], ("rest", "--method", "get"))

    def run_main(self, action, confirm_failure=False):
        self.lab_mock("discover", return_value={TENANT: {}})
        self.lab_mock("resolver_targets", return_value=[TARGET])
        self.lab_mock("current_account")
        self.lab_mock("check_resolvers")
        self.lab_mock("confirm", side_effect=ValueError("Cancelled") if confirm_failure else None)
        return forwarding.main([
            action, "--central-tenant-id", CENTRAL, "--tenant-id", TENANT,
            "--resolver-map", str(self.map_path), "--rules-file", str(self.rules_path),
        ])

    def test_previews_and_confirmation_failure_never_write(self):
        for action in ("validate", "what-if"):
            with self.subTest(action=action):
                self.fixtures()
                apply = self.mock("apply")
                self.assertEqual(self.run_main(action), 0)
                apply.assert_not_called()
                forwarding.lab.confirm.assert_not_called()
        self.fixtures()
        apply = self.mock("apply")
        self.assertEqual(self.run_main("create", confirm_failure=True), 1)
        apply.assert_not_called()

    def test_export_round_trip_preserves_paged_rules_states_and_server_priority(self):
        self.lab_mock("current_account")
        self.lab_mock("live_subscription")
        first = rule(domain="CORP.EXAMPLE.")
        second = rule(domain=".", name="wildcard", state="Disabled")
        next_url = f"{forwarding.lab.ARM}{RULESET}/forwardingRules?api-version={forwarding.API}&page=2"

        def az(*args):
            self.assertEqual(args[:3], ("rest", "--method", "get"))
            self.assertEqual(args[-2:], ("--subscription", SUB))
            url = args[args.index("--url") + 1]
            if "/forwardingRules?" not in url:
                return resource(RULESET)
            if url == next_url:
                return {"value": [second]}
            return {"value": [first], "nextLink": next_url}

        azure = self.lab_mock("az", side_effect=az)
        directory = self.rules_path.parent / "inventory"
        self.assertEqual(forwarding.main([
            "export", "--central-tenant-id", CENTRAL, "--source-ruleset-id", RULESET,
            "--inventory-directory", str(directory),
        ]), 0)
        output = directory / "lab03-forwarding-rules.json"
        snapshot = json.loads(output.read_text())
        self.assertEqual(snapshot["sourceRulesetId"], RULESET)
        self.assertEqual(snapshot["sourceTenantId"], CENTRAL)
        self.assertEqual(forwarding.load_rules(output), {
            **RULES, ".": {"servers": SERVERS, "state": "Disabled"},
        })
        forwarding.lab.current_account.assert_called_once_with(CENTRAL, SUB, human=True)
        forwarding.lab.live_subscription.assert_called_once_with(SUB, CENTRAL)
        self.assertEqual(azure.call_count, 3)

    def test_export_failure_preserves_existing_snapshot(self):
        self.rules_path.write_text("previous snapshot")
        self.lab_mock("current_account")
        self.lab_mock("live_subscription")
        self.mock("get_resource", return_value=resource(RULESET))
        for values in ([], [rule(), rule(name="duplicate")], [rule(state="Invalid")]):
            with self.subTest(values=values):
                self.mock("listing", return_value=values)
                with self.assertRaises(ValueError):
                    forwarding.export_rules(CENTRAL, RULESET, self.rules_path)
                self.assertEqual(self.rules_path.read_text(), "previous snapshot")

    def test_export_requires_central_context_before_reading_or_writing(self):
        self.lab_mock("current_account", side_effect=ValueError("Wrong signed-in tenant"))
        azure = self.lab_mock("az")
        output = self.rules_path.parent / "not-created.json"
        with self.assertRaisesRegex(ValueError, "Wrong signed-in tenant"):
            forwarding.export_rules(CENTRAL, RULESET, output)
        azure.assert_not_called()
        self.assertFalse(output.exists())

    def test_disabled_exported_rule_is_created_disabled_and_then_skipped(self):
        self.fixtures()
        desired = {"corp.example.": {"servers": SERVERS, "state": "Disabled"}}
        plan = forwarding.plan_target(TARGET, {}, desired)
        self.assertEqual(plan["rules"][0]["state"], "Disabled")
        writes = self.apply_calls(plan)
        forwarding.apply(plan)
        self.assertEqual(writes[1][writes[1].index("--forwarding-rule-state") + 1], "Disabled")
        self.fixtures(existing=True, linked=True, rules=[rule(state="Disabled")])
        rerun = forwarding.plan_target(TARGET, {}, desired)
        self.assertFalse(rerun["create"] or rerun["link"] or rerun["rules"])

    def test_all_tenants_use_saved_file_and_preflight_before_confirmation(self):
        second_tenant = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        second_sub = "33333333-3333-3333-3333-333333333333"
        second = {**TARGET, "tenantId": second_tenant, "subscriptionId": second_sub,
                  "resolverVnetId": VNET.replace(SUB, second_sub)}
        self.map_path.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET, second]}))
        saved = self.rules_path.parent / "lab03-forwarding-rules.json"
        saved.write_text(self.rules_path.read_text())
        self.lab_mock("discover", return_value={TENANT: {}, second_tenant: {}})
        self.lab_mock("resolver_targets", return_value=[TARGET, second])
        account = self.lab_mock("current_account")
        self.lab_mock("check_resolvers")
        confirm = self.lab_mock("confirm")
        apply = self.mock("apply")

        def plan(target, options, requested):
            self.assertEqual(requested, RULES)
            confirm.assert_not_called()
            apply.assert_not_called()
            return {"id": RULESET.replace(SUB, target["subscriptionId"]), "create": True, "link": True,
                    "endpoint": {"id": ENDPOINT.replace(SUB, target["subscriptionId"])},
                    "vnet": target["resolverVnetId"], "rules": [
                        {"domain": domain, "servers": value["servers"], "state": value["state"]}
                        for domain, value in requested.items()
                    ]}

        planner = self.mock("plan_target", side_effect=plan)
        args = ["create", "--central-tenant-id", CENTRAL, "--inventory-directory", str(self.rules_path.parent),
                "--resolver-map", str(self.map_path)]
        self.assertEqual(forwarding.main(args), 0)
        self.assertEqual(planner.call_count, 2)
        account.assert_any_call(TENANT, SUB, human=True)
        account.assert_any_call(second_tenant, second_sub, human=True)
        confirm.assert_called_once_with("forwarding", f"{TENANT},{second_tenant}")
        self.assertEqual(apply.call_count, 2)

    def test_all_tenant_preflight_failure_prevents_partial_creation(self):
        second = {**TARGET, "resolverVnetId": VNET + "-second"}
        self.map_path.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET, second]}))
        self.lab_mock("discover", return_value={TENANT: {}})
        self.lab_mock("resolver_targets", return_value=[TARGET, second])
        self.lab_mock("current_account")
        self.lab_mock("check_resolvers")
        confirm = self.lab_mock("confirm")
        apply = self.mock("apply")
        self.mock("plan_target", side_effect=[{}, ValueError("conflicting second resolver")])
        self.assertEqual(forwarding.main([
            "create", "--central-tenant-id", CENTRAL, "--resolver-map", str(self.map_path),
            "--rules-file", str(self.rules_path),
        ]), 1)
        confirm.assert_not_called()
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lab04_align", ROOT / "scripts/align_dns.py")
align = importlib.util.module_from_spec(spec)
spec.loader.exec_module(align)
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB = "11111111-1111-1111-1111-111111111111"
GROUP = f"/subscriptions/{SUB}/resourceGroups/network"
HUB = GROUP + "/providers/Microsoft.Network/virtualNetworks/hub"
SPOKE = GROUP + "/providers/Microsoft.Network/virtualNetworks/spoke"
SECOND = GROUP + "/providers/Microsoft.Network/virtualNetworks/second"
ORPHAN = GROUP + "/providers/Microsoft.Network/virtualNetworks/orphan"
TRANSITIVE = GROUP + "/providers/Microsoft.Network/virtualNetworks/transitive"
RESOLVER = f"/subscriptions/{SUB}/resourceGroups/resolver-network/providers/Microsoft.Network/virtualNetworks/resolver"
RESOLVER_RESOURCE = f"/subscriptions/{SUB}/resourceGroups/resolver-network/providers/Microsoft.Network/dnsResolvers/resolver"
ENDPOINT = RESOLVER_RESOURCE + "/inboundEndpoints/inbound"
TARGET = {"tenantId": TENANT, "subscriptionId": SUB, "resolverVnetId": RESOLVER}
IP = "10.10.1.4"


def network(resource_id, peers=()):
    match = align.vnet_parts(resource_id)
    return {
        "id": resource_id, "name": resource_id.rsplit("/", 1)[-1],
        "subscriptionId": match["subscription"], "resourceGroup": match["group"],
        "peerings": [{"remoteVirtualNetwork": {"id": remote}, "peeringState": state} for remote, state in peers],
    }


def document():
    outside = HUB.replace(SUB, "99999999-9999-9999-9999-999999999999")
    return {"tenantId": TENANT, "hubVnetId": HUB, "virtualNetworks": [
        network(HUB, [(RESOLVER, "Connected"), (SPOKE, "Disconnected"), (outside, "Connected")]),
        network(RESOLVER, [(HUB, "Connected")]),
        network(SPOKE),
        network(SECOND, [(HUB, "Initiated")]),
        network(ORPHAN),
        network(TRANSITIVE, [(SPOKE, "Connected")]),
    ]}


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.map = self.directory / "resolvers.json"
        self.map.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET]}))
        self.doc = document()
        self.affected = {HUB, RESOLVER, SPOKE, SECOND}
        self.resources = {
            value["id"]: {
                "id": value["id"], "etag": '"before"', "provisioningState": "Succeeded",
                "dhcpOptions": {"dnsServers": []}, "tags": {"preserve": "yes"},
                "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
            } for value in self.doc["virtualNetworks"]
        }
        self.peers = {value["id"]: [item["remoteVirtualNetwork"]["id"] for item in value["peerings"]]
                      for value in self.doc["virtualNetworks"]}
        self.endpoint = {
            "id": ENDPOINT, "name": "inbound", "properties": {
                "provisioningState": "Succeeded", "ipConfigurations": [{
                    "privateIpAddress": IP, "subnet": {"id": RESOLVER + "/subnets/inbound"},
                }],
            },
        }
        self.endpoints = [self.endpoint]
        self.calls = []
        self.write_failure = None
        self.mock_common("az", side_effect=self.azure)
        self.mock_common("permissions")

    def mock_common(self, name, **kwargs):
        return self.stack.enter_context(patch.object(align.common, name, **kwargs))

    def azure(self, *args):
        self.calls.append(args)
        if args[:3] == ("network", "vnet", "show"):
            resource_id = args[args.index("--ids") + 1]
            self.assertIn(resource_id, self.affected)
            return deepcopy(self.resources[resource_id])
        if args[:4] == ("network", "vnet", "peering", "list"):
            group = args[args.index("--resource-group") + 1]
            name = args[args.index("--vnet-name") + 1]
            resource_id = f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Network/virtualNetworks/{name}"
            self.assertIn(resource_id, self.affected)
            return [{"remoteVirtualNetwork": {"id": remote}} for remote in self.peers[resource_id]]
        if args[:3] == ("network", "vnet", "update"):
            resource_id = args[args.index("--ids") + 1]
            self.assertIn(resource_id, self.affected)
            self.assertEqual(args[:7], ("network", "vnet", "update", "--subscription", SUB, "--ids", resource_id))
            self.assertEqual(args[7], "--dns-servers")
            if resource_id == self.write_failure:
                raise ValueError("DNS update rejected")
            dns = list(args[8:])
            self.resources[resource_id]["dhcpOptions"]["dnsServers"] = [] if dns == ["null"] else dns
            self.resources[resource_id]["etag"] = '"after"'
            return {}
        if args[0] == "rest":
            self.assertEqual(args[-2:], ("--subscription", SUB))
            url = args[args.index("--url") + 1]
            if "/dnsResolvers?" in url:
                return {"value": [{
                    "id": RESOLVER_RESOURCE, "name": "resolver",
                    "properties": {"provisioningState": "Succeeded", "virtualNetwork": {"id": RESOLVER}},
                }]}
            if "/inboundEndpoints?" in url:
                return {"value": deepcopy(self.endpoints)}
            if f"{ENDPOINT}?" in url:
                return deepcopy(self.endpoint)
        self.fail(f"Unexpected Azure CLI request: {args}")

    def plan(self):
        target = {**TARGET, "resourceGroup": "resolver-network"}
        return align.plan_tenant(target, {}, self.doc)

    def writes(self):
        return [args for args in self.calls if args[:3] == ("network", "vnet", "update")]

    def run_main(self, action, confirmation_failure=False):
        self.mock_common("discover", return_value={TENANT: self.doc})
        self.mock_common("current_account")
        self.mock_common("live_subscription")
        self.mock_common("confirm", side_effect=ValueError("Cancelled") if confirmation_failure else None)
        return align.main([action, "--inventory-directory", str(self.directory), "--resolver-map", str(self.map)])

    def test_inventory_fence_keeps_only_hub_and_direct_inventoried_spokes(self):
        hub, resolver, spokes = align.topology(self.doc, RESOLVER)
        self.assertEqual(hub["id"], HUB)
        self.assertEqual(resolver["id"], RESOLVER)
        self.assertEqual({value["id"] for value in spokes}, {SPOKE, SECOND})
        self.assertNotIn(ORPHAN, {value["id"] for value in spokes})
        self.assertNotIn(TRANSITIVE, {value["id"] for value in spokes})

    def test_reviewed_hub_and_resolver_must_exist_and_be_distinct(self):
        for hub in (None, HUB + "-absent", RESOLVER):
            with self.subTest(hub=hub):
                self.doc["hubVnetId"] = hub
                with self.assertRaises(ValueError):
                    align.topology(self.doc, RESOLVER)

    def test_plan_uses_same_tenant_inbound_ip_and_preserves_resolver_azure_dns(self):
        rows = self.plan()
        self.assertEqual({row["id"] for row in rows}, self.affected)
        self.assertEqual(rows[0]["role"], "resolver")
        for row in rows:
            self.assertEqual(row["dnsAfter"], [] if row["id"] == RESOLVER else [IP])
        self.assertFalse(rows[0]["changed"])
        self.assertTrue(all(row["changed"] for row in rows[1:]))

    def test_removed_live_inventory_peering_stops_before_dns_changes(self):
        self.peers[HUB].remove(SPOKE)
        with self.assertRaisesRegex(ValueError, "no longer present live"):
            self.plan()
        self.assertEqual(self.writes(), [])

    def test_multiple_inbound_endpoints_require_explicit_choice(self):
        second = deepcopy(self.endpoint)
        second["id"] = ENDPOINT + "-second"
        second["name"] = "inbound-second"
        self.endpoints.append(second)
        target = {**TARGET, "resourceGroup": "resolver-network"}
        with self.assertRaisesRegex(ValueError, "inboundEndpointId"):
            align.inbound_endpoint(target, {})
        chosen = align.inbound_endpoint(target, {"inboundEndpointId": ENDPOINT})
        self.assertEqual(chosen, {"id": ENDPOINT, "dns": [IP]})

    def test_invalid_or_foreign_inbound_configuration_stops_preflight(self):
        for mutation in (
            {"privateIpAddress": "0.0.0.0"},
            {"subnet": {"id": HUB + "/subnets/inbound"}},
        ):
            with self.subTest(mutation=mutation):
                endpoint = deepcopy(self.endpoint)
                endpoint["properties"]["ipConfigurations"][0].update(mutation)
                with self.assertRaises(ValueError):
                    align.endpoint_ips(endpoint, RESOLVER)

    def test_read_only_actions_and_cancel_never_change_dns_or_create_backups(self):
        for action in ("validate", "what-if"):
            with self.subTest(action=action):
                self.assertEqual(self.run_main(action), 0)
                self.assertEqual(self.writes(), [])
                self.assertEqual(list(self.directory.glob("lab04-dns-backup-*.json")), [])
                align.common.confirm.assert_not_called()
        self.assertEqual(self.run_main("create", confirmation_failure=True), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(list(self.directory.glob("lab04-dns-backup-*.json")), [])

    def test_create_resets_resolver_then_changes_only_hub_and_direct_spoke_dns(self):
        self.resources[RESOLVER]["dhcpOptions"]["dnsServers"] = ["10.99.0.10"]
        self.assertEqual(self.run_main("create"), 0)
        writes = self.writes()
        self.assertEqual(writes[0][-1], "null")
        self.assertEqual(writes[0][6], RESOLVER)
        self.assertEqual({args[6] for args in writes}, self.affected)
        for resource_id, resource in self.resources.items():
            expected = [IP] if resource_id in (HUB, SPOKE, SECOND) else []
            self.assertEqual(resource["dhcpOptions"]["dnsServers"], expected)
            self.assertEqual(resource["tags"], {"preserve": "yes"})
            self.assertEqual(resource["addressSpace"], {"addressPrefixes": ["10.0.0.0/16"]})
        backups = list(self.directory.glob("lab04-dns-backup-*.json"))
        self.assertEqual(len(backups), 1)
        snapshot = json.loads(backups[0].read_text())
        self.assertEqual(snapshot["changes"][0]["dnsBefore"], ["10.99.0.10"])
        self.assertTrue(all(row["status"] == "verified" for row in snapshot["changes"]))

    def test_matching_dns_is_noop_and_does_not_create_another_backup(self):
        for resource_id in (HUB, SPOKE, SECOND):
            self.resources[resource_id]["dhcpOptions"]["dnsServers"] = [IP]
        self.assertEqual(self.run_main("create"), 0)
        self.assertEqual(self.writes(), [])
        align.common.confirm.assert_not_called()
        self.assertEqual(list(self.directory.glob("lab04-dns-backup-*.json")), [])

    def test_missing_permissions_and_changed_etag_prevent_mutation(self):
        align.common.permissions.side_effect = ValueError("Missing write permission")
        self.assertEqual(self.run_main("create"), 1)
        self.assertEqual(self.writes(), [])
        align.common.permissions.side_effect = None
        rows = [row for row in self.plan() if row["changed"]]
        self.resources[rows[0]["id"]]["etag"] = '"concurrently-changed"'
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            align.apply(rows, self.directory, [TENANT])
        self.assertEqual(self.writes(), [])

    def test_changed_inbound_ip_prevents_writing_stale_dns(self):
        rows = [row for row in self.plan() if row["changed"]]
        self.endpoint["properties"]["ipConfigurations"][0]["privateIpAddress"] = "10.10.1.5"
        with self.assertRaisesRegex(ValueError, "IP changed after preview"):
            align.apply(rows, self.directory, [TENANT])
        self.assertEqual(self.writes(), [])
        self.assertEqual(list(self.directory.glob("lab04-dns-backup-*.json")), [])

    def test_partial_failure_keeps_backup_and_stops_later_updates(self):
        rows = [row for row in self.plan() if row["changed"]]
        self.write_failure = rows[1]["id"]
        with self.assertRaisesRegex(ValueError, "DNS update rejected"):
            align.apply(rows, self.directory, [TENANT])
        self.assertEqual(len(self.writes()), 2)
        backup = json.loads(next(self.directory.glob("lab04-dns-backup-*.json")).read_text())
        self.assertEqual([row["status"] for row in backup["changes"]], ["verified", "updating", "planned"])
        self.assertEqual(self.resources[rows[0]["id"]]["dhcpOptions"]["dnsServers"], [IP])

    def test_all_tenants_include_central_inventory_and_require_one_resolver_each(self):
        other_tenant = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        other_sub = "22222222-2222-2222-2222-222222222222"
        other_doc = json.loads(json.dumps(self.doc).replace(TENANT, other_tenant).replace(SUB, other_sub))
        other_target = {**TARGET, "tenantId": other_tenant, "subscriptionId": other_sub,
                        "resolverVnetId": RESOLVER.replace(SUB, other_sub)}
        self.map.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET, other_target]}))
        discovery = self.mock_common("discover", return_value={TENANT: self.doc, other_tenant: other_doc})
        accounts = self.mock_common("current_account")
        self.mock_common("live_subscription")
        self.mock_common("confirm")
        with patch.object(align, "plan_tenant", return_value=[]) as planner:
            self.assertEqual(align.main([
                "validate", "--inventory-directory", str(self.directory), "--resolver-map", str(self.map),
            ]), 0)
            self.assertEqual(planner.call_count, 2)
        discovery.assert_called_once_with(self.directory)
        accounts.assert_any_call(TENANT, SUB, human=True)
        accounts.assert_any_call(other_tenant, other_sub, human=True)
        duplicate = {**TARGET, "resolverVnetId": HUB}
        self.map.write_text(json.dumps({"schemaVersion": 1, "resolvers": [TARGET, other_target, duplicate]}))
        self.assertEqual(align.main([
            "validate", "--inventory-directory", str(self.directory), "--resolver-map", str(self.map),
        ]), 1)

    def generation_fixture(self):
        other_tenant = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        other_sub = "22222222-2222-2222-2222-222222222222"
        other_doc = json.loads(json.dumps(self.doc).replace(TENANT, other_tenant).replace(SUB, other_sub))
        entry = {
            "tenantId": other_tenant, "subscriptionId": other_sub,
            "resolverVnetId": RESOLVER.replace(SUB, other_sub),
            "inboundEndpointId": ENDPOINT.replace(SUB, other_sub),
            "outboundEndpointId": RESOLVER_RESOURCE.replace(SUB, other_sub) + "/outboundEndpoints/outbound",
        }
        source = self.directory / "lab03-resolvers.json"
        source.write_text(json.dumps({"schemaVersion": 1, "resolvers": [entry]}))
        self.mock_common("discover", return_value={TENANT: self.doc, other_tenant: other_doc})
        args = [
            "generate-map", "--inventory-directory", str(self.directory),
            "--central-tenant-id", TENANT, "--central-resolver-vnet-id", RESOLVER,
        ]
        return args, source, entry

    def test_generate_map_preserves_source_selectors_and_adds_central_without_azure_calls(self):
        args, source, entry = self.generation_fixture()
        original = source.read_bytes()
        self.assertEqual(align.main(args + ["--central-inbound-endpoint-id", ENDPOINT]), 0)
        generated = json.loads((self.directory / "lab04-resolvers.json").read_text())
        self.assertEqual(generated, {"schemaVersion": 1, "resolvers": [
            entry, {**TARGET, "inboundEndpointId": ENDPOINT},
        ]})
        self.assertEqual(source.read_bytes(), original)
        align.common.az.assert_not_called()
        self.assertEqual(self.writes(), [])

    def test_generation_protects_existing_output_and_source_even_with_overwrite(self):
        args, source, _ = self.generation_fixture()
        original = source.read_bytes()
        output = self.directory / "custom-map.json"
        output.write_text('{"previous":"map"}')
        custom = args + ["--source-resolver-map", str(source), "--output-file", str(output)]
        self.assertEqual(align.main(custom), 1)
        self.assertEqual(output.read_text(), '{"previous":"map"}')
        self.assertEqual(align.main(custom + ["--overwrite"]), 0)
        self.assertEqual(json.loads(output.read_text())["resolvers"][-1], TARGET)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(align.main(args + ["--output-file", str(source), "--overwrite"]), 1)
        self.assertEqual(source.read_bytes(), original)
        align.common.az.assert_not_called()

    def test_invalid_generation_inputs_leave_existing_output_unchanged(self):
        args, source, entry = self.generation_fixture()
        output = self.directory / "lab04-resolvers.json"
        output.write_text('{"previous":"map"}')
        cases = [
            ([], []),
            ([TARGET], []),
            ([{**entry, "resolverVnetId": RESOLVER}], []),
            ([{**entry, "resolverVnetId": entry["resolverVnetId"] + "-missing"}], []),
            ([entry, {**entry, "resolverVnetId": HUB.replace(SUB, entry["subscriptionId"])}], []),
            ([entry], ["--central-resolver-vnet-id", HUB]),
            ([entry], ["--central-inbound-endpoint-id", ENDPOINT.replace(SUB, entry["subscriptionId"])]),
            ([{**entry, "inboundEndpointId": "invalid"}], []),
        ]
        for entries, extra in cases:
            with self.subTest(entries=entries, extra=extra):
                source.write_text(json.dumps({"schemaVersion": 1, "resolvers": entries}))
                original = source.read_bytes()
                self.assertEqual(align.main(args + extra + ["--overwrite"]), 1)
                self.assertEqual(output.read_text(), '{"previous":"map"}')
                self.assertEqual(source.read_bytes(), original)
        align.common.az.assert_not_called()


if __name__ == "__main__":
    unittest.main()

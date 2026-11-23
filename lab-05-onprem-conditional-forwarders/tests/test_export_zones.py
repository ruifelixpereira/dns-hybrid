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
spec = importlib.util.spec_from_file_location("lab05_export", ROOT / "scripts/export_zones.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB = "11111111-1111-1111-1111-111111111111"
SECOND = "22222222-2222-2222-2222-222222222222"
FOREIGN = "33333333-3333-3333-3333-333333333333"
RESOLVER = f"/subscriptions/{SUB}/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver"
IP = "10.10.1.4"
ZONE_A = f"/subscriptions/{SUB}/resourceGroups/dns/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"
ZONE_B = f"/subscriptions/{SECOND}/resourceGroups/dns/providers/Microsoft.Network/privateDnsZones/privatelink.file.core.windows.net"
UNRELATED = f"/subscriptions/{SUB}/resourceGroups/dns/providers/Microsoft.Network/privateDnsZones/unrelated.example"


def zone(resource_id):
    return {"id": resource_id, "name": resource_id.rsplit("/", 1)[-1],
            "properties": {"provisioningState": "Succeeded"}}


def link(zone_id, vnet=RESOLVER):
    return {"id": zone_id + "/virtualNetworkLinks/resolver", "name": "resolver", "properties": {
        "virtualNetwork": {"id": vnet}, "provisioningState": "Succeeded", "virtualNetworkLinkState": "Completed",
    }}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stdout = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.output = self.directory / "forwarders.json"
        self.accounts = [
            {"id": SUB, "tenantId": TENANT, "state": "Enabled"},
            {"id": SECOND, "tenantId": TENANT, "state": "Enabled"},
            {"id": FOREIGN, "tenantId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "state": "Enabled"},
            {"id": "44444444-4444-4444-4444-444444444444", "tenantId": TENANT, "state": "Disabled"},
        ]
        self.responses = {}
        self.zone_list = {}
        for subscription, values in ((SUB, [zone(ZONE_A)]), (SECOND, [zone(ZONE_B)])):
            url = f"{export.common.ARM}/subscriptions/{subscription}/providers/Microsoft.Network/privateDnsZones?api-version={export.ZONE_API}"
            self.zone_list[subscription] = url
            self.responses[url] = {"value": values}
        next_zones = self.zone_list[SUB] + "&skip=next"
        self.responses[self.zone_list[SUB]]["nextLink"] = next_zones
        self.responses[next_zones] = {"value": [zone(UNRELATED)]}
        for resource_id in (ZONE_A, ZONE_B, UNRELATED):
            url = f"{export.common.ARM}{resource_id}/virtualNetworkLinks?api-version={export.ZONE_API}"
            self.responses[url] = {"value": [link(resource_id, RESOLVER + "-other")]}
            if resource_id != UNRELATED:
                self.responses[url]["nextLink"] = url + "&skip=next"
                self.responses[url + "&skip=next"] = {"value": [link(resource_id)]}
        self.ips = [IP]
        self.az = self.stack.enter_context(patch.object(export.common, "az", side_effect=self.azure))
        self.contexts = self.stack.enter_context(patch.object(export.common, "current_account"))
        self.stack.enter_context(patch.object(export.common, "live_subscription"))
        self.read_vnet = self.stack.enter_context(patch.object(export.alignment, "read_vnet"))
        self.selection = self.stack.enter_context(patch.object(export.alignment, "inbound_endpoint"))

    def azure(self, *args):
        if args[:3] == ("account", "list", "--all"):
            return deepcopy(self.accounts)
        self.assertEqual(args[:3], ("rest", "--method", "get"))
        url = args[args.index("--url") + 1]
        self.assertNotIn(FOREIGN, url)
        return deepcopy(self.responses[url])

    def collect(self):
        return export.collect(TENANT, RESOLVER, self.ips)

    def command(self, action):
        return export.main([action, "--central-tenant-id", TENANT, "--resolver-vnet-id", RESOLVER,
                            "--inbound-ip", *self.ips, "--output-file", str(self.output)])

    def test_collects_paged_exact_links_across_only_central_enabled_subscriptions(self):
        result = self.collect()
        self.assertEqual(result["subscriptionIds"], [SUB, SECOND])
        self.assertEqual(result["dnsServers"], [IP])
        self.assertEqual([value["id"] for value in result["zones"]], [ZONE_A, ZONE_B])
        self.contexts.assert_any_call(TENANT, SUB, human=True)
        self.contexts.assert_any_call(TENANT, SECOND, human=True)
        self.selection.assert_not_called()

    def test_no_matching_links_returns_empty_plan_without_selecting_other_vnets(self):
        for resource_id in (ZONE_A, ZONE_B):
            url = f"{export.common.ARM}{resource_id}/virtualNetworkLinks?api-version={export.ZONE_API}&skip=next"
            self.responses[url]["value"][0]["properties"]["virtualNetwork"]["id"] = RESOLVER + "-other"
        self.assertEqual(self.collect()["zones"], [])

    def test_wrong_tenant_resolver_subscription_fails_before_resolver_reads(self):
        self.accounts[0]["tenantId"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        with self.assertRaisesRegex(ValueError, "central tenant"):
            self.collect()
        self.read_vnet.assert_not_called()

    def test_bad_links_duplicate_zones_or_wrong_subscription_preserve_previous_export(self):
        original = deepcopy(self.responses)
        linked_url = f"{export.common.ARM}{ZONE_A}/virtualNetworkLinks?api-version={export.ZONE_API}&skip=next"
        for failure in ("unhealthy", "duplicate", "wrong-subscription"):
            with self.subTest(failure=failure):
                self.responses = deepcopy(original)
                self.output.write_text('{"previous":"snapshot"}')
                if failure == "unhealthy":
                    self.responses[linked_url]["value"][0]["properties"]["virtualNetworkLinkState"] = "InProgress"
                elif failure == "duplicate":
                    self.responses[self.zone_list[SUB]]["value"].append(zone(ZONE_A))
                else:
                    self.responses[self.zone_list[SUB]]["value"] = [zone(ZONE_B)]
                self.assertEqual(self.command("export"), 1)
                self.assertEqual(self.output.read_text(), '{"previous":"snapshot"}')

    def test_invalid_ips_and_read_failure_do_not_replace_previous_export(self):
        self.output.write_text('{"previous":"snapshot"}')
        for ips in (
            ["not-an-ip"], ["::1"], ["168.63.129.16"], ["0.0.0.0"], ["127.0.0.1"],
            ["169.254.1.1"], ["224.0.0.1"], ["255.255.255.255"], [IP, IP],
        ):
            with self.subTest(ips=ips):
                self.ips = ips
                self.assertEqual(self.command("export"), 1)
                self.assertEqual(self.output.read_text(), '{"previous":"snapshot"}')
                self.az.assert_not_called()
        self.ips = [IP]
        self.az.side_effect = ValueError("Azure access denied")
        self.assertEqual(self.command("export"), 1)
        self.assertEqual(self.output.read_text(), '{"previous":"snapshot"}')

    def test_discover_outputs_only_json_and_export_persists_the_same_shape(self):
        self.assertEqual(self.command("discover"), 0)
        document = json.loads(self.stdout.getvalue())
        self.assertEqual(document["dnsServers"], [IP])
        self.assertNotIn("inboundEndpointId", document)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.command("export"), 0)
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["zones"], document["zones"])
        self.assertEqual(saved["centralTenantId"], TENANT)
        self.assertEqual(saved["schemaVersion"], 2)

    def test_cli_preserves_supplied_ip_priority_without_endpoint_discovery(self):
        self.ips = ["10.10.1.5", IP]
        self.assertEqual(self.command("export"), 0)
        self.assertEqual(json.loads(self.output.read_text())["dnsServers"], self.ips)
        self.selection.assert_not_called()
        self.assertFalse(any("/dnsResolvers/" in str(call) for call in self.az.call_args_list))

    def test_empty_ip_list_stops_before_azure_reads(self):
        self.ips = []
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.collect()
        self.az.assert_not_called()


if __name__ == "__main__":
    unittest.main()

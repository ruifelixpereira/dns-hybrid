import csv
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("central_deploy", ROOT / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
ORIGINAL_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
GROUP = "central-dns"
VNET = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/resolver"


def row(name, operation, tenant=OTHER):
    return {
        "privateDnsZoneName": name,
        "tenantId": tenant,
        "privateDnsZoneId": (
            f"/subscriptions/{ORIGINAL_SUBSCRIPTION}/resourceGroups/original-dns"
            f"/providers/Microsoft.Network/privateDnsZones/{name}"
        ),
        "privateDnsZoneSubscriptionId": ORIGINAL_SUBSCRIPTION,
        "privateDnsZoneResourceGroup": "original-dns",
        "centralTenantOperation": operation,
        "originalTenantOperation": "none" if operation == "none" else "delete",
    }


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "plan.csv"

    def write_plan(self, rows):
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(reversed(deploy.COLUMNS)))
            writer.writeheader()
            writer.writerows(rows)

    def test_separates_create_zones_from_all_normalized_csv_names(self):
        self.write_plan([
            row("Private.Example.", "create"), row("private.example", "create"),
            row("existing.example", "none", TENANT), row("repeated.example", "repeated"),
        ])
        self.assertEqual(deploy.load_zone_names(self.path, TENANT), (
            ["private.example"], {"private.example", "existing.example", "repeated.example"},
        ))

    def test_rejects_unsafe_zone_names(self):
        for name in ("../zone", "zone/name", "zone\\name", "zone\nname", ".", "a..example", "a" * 64 + ".example"):
            with self.subTest(name=name):
                self.write_plan([row(name, "create")])
                with self.assertRaisesRegex(ValueError, "invalid private DNS zone name"):
                    deploy.load_zone_names(self.path, TENANT)

    def test_rejects_inconsistent_operations_and_central_tenant(self):
        valid = row("private.example", "create")
        mutations = (
            {"centralTenantOperation": "delete"},
            {"tenantId": TENANT},
            {"tenantId": "invalid"},
            {"originalTenantOperation": "create"},
            {"privateDnsZoneSubscriptionId": "invalid"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = {**valid, **mutation}
                self.write_plan([invalid])
                with self.assertRaises(ValueError):
                    deploy.load_zone_names(self.path, TENANT)
        self.write_plan([valid, row("PRIVATE.EXAMPLE.", "none", TENANT)])
        with self.assertRaisesRegex(ValueError, "both create and none"):
            deploy.load_zone_names(self.path, TENANT)

    def test_rejects_malformed_csv(self):
        for rows in (
            [["wrong", "headers"]],
            [list(deploy.COLUMNS), ["short"]],
            [list(deploy.COLUMNS) + ["tenantId"]],
        ):
            with self.subTest(rows=rows):
                with self.path.open("w", newline="") as stream:
                    csv.writer(stream).writerows(rows)
                with self.assertRaises(ValueError):
                    deploy.load_zone_names(self.path, TENANT)


class DeploymentTests(unittest.TestCase):
    exporters = ("bash", "powershell")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.plan = self.directory / "central plan.csv"
        self.log = self.directory / "calls.jsonl"
        mock_bin = self.directory / "mock bin"
        mock_bin.mkdir()
        az = mock_bin / "az"
        az.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
            shlex.quote(str(ROOT / "tests/mock_az.py")) + ' "$@"\n'
        )
        az.chmod(0o700)
        self.environment = {
            **os.environ, "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"],
            "MOCK_LOG": str(self.log), "MOCK_TENANT": TENANT,
            "MOCK_SUBSCRIPTION": SUBSCRIPTION, "MOCK_RESOURCE_GROUP": GROUP,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.write_plan([
            row("new.example", "create"), row("existing.example", "none", TENANT),
            row("repeated.example", "repeated"),
        ])

    def write_plan(self, rows):
        with self.plan.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=deploy.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def run_deploy(self, exporter, action="validate", confirmation="", mode=""):
        if exporter == "powershell":
            pwsh = os.environ.get("PWSH") or shutil.which("pwsh")
            if not pwsh:
                self.skipTest("PowerShell 7.4+ is not installed; set PWSH to its executable")
            command = [
                pwsh, "-NoLogo", "-NoProfile", "-File", str(ROOT / "scripts/Deploy.ps1"),
                "-Action", action, "-TenantId", TENANT, "-SubscriptionId", SUBSCRIPTION,
                "-ResourceGroup", GROUP, "-InputCsv", str(self.plan),
                "-ResolverVnetId", VNET,
            ]
        else:
            command = [
                "bash", str(ROOT / "scripts/deploy.sh"), action,
                "--tenant-id", TENANT, "--subscription-id", SUBSCRIPTION,
                "--resource-group", GROUP, "--input-csv", str(self.plan),
                "--resolver-vnet-id", VNET,
            ]
        if self.log.exists():
            self.log.unlink()
        result = subprocess.run(
            command, input=confirmation, capture_output=True, text=True, timeout=30,
            env={**self.environment, "MOCK_MODE": mode},
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_validate_and_what_if_deploy_only_create_zones_to_explicit_scope(self):
        for exporter in self.exporters:
            for action in ("validate", "what-if"):
                with self.subTest(exporter=exporter, action=action):
                    result, calls = self.run_deploy(exporter, action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    deployments = [call for call in calls if call["args"][:2] == ["deployment", "group"]]
                    self.assertEqual(len(deployments), 1)
                    deployment = deployments[0]
                    self.assertEqual(deployment["args"][:3], ["deployment", "group", action])
                    self.assertEqual(deployment["parameters"]["parameters"]["zoneNames"]["value"], ["new.example"])
                    self.assertEqual(deployment["parameters"]["parameters"]["resolverVnetId"]["value"], VNET)
                    links = [
                        link for call in deployments
                        for link in call["parameters"]["parameters"]["vnetLinks"]["value"]
                    ]
                    self.assertEqual({link["zoneName"] for link in links}, {"new.example"})
                    self.assertTrue(all(link["name"].startswith("resolver-") for link in links))
                    parameter_file = deployment["args"][deployment["args"].index("--parameters") + 1]
                    self.assertFalse(Path(parameter_file[1:]).exists())

    def test_create_requires_exact_destination_confirmation(self):
        for exporter in self.exporters:
            for confirmation, succeeds in (
                ("wrong\n", False), ("", False), (f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", True),
            ):
                with self.subTest(exporter=exporter, confirmation=confirmation):
                    result, calls = self.run_deploy(exporter, "create", confirmation)
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    self.assertEqual(len(calls), 6 if succeeds else 5)
                    if not succeeds:
                        self.assertIn("cancelled", result.stderr)

    def test_wrong_tenant_and_missing_resource_group_stop_deployment(self):
        for exporter in self.exporters:
            for mode, expected_calls in (("tenant-mismatch", 1), ("missing-group", 2)):
                with self.subTest(exporter=exporter, mode=mode):
                    result, calls = self.run_deploy(exporter, mode=mode)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Error:", result.stderr)
                    self.assertEqual(len(calls), expected_calls)

    def test_no_create_operations_still_check_existing_links(self):
        self.write_plan([row("existing.example", "none", TENANT), row("repeated.example", "repeated")])
        for exporter in self.exporters:
            with self.subTest(exporter=exporter):
                result, calls = self.run_deploy(exporter, "create", mode="all-linked")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 5)
                self.assertFalse(any(call["args"][:2] == ["deployment", "group"] for call in calls))
                self.assertIn("No create operations", result.stderr)

    def test_links_are_created_without_zone_create_operations(self):
        self.write_plan([row("existing.example", "none", TENANT)])
        for exporter in self.exporters:
            with self.subTest(exporter=exporter):
                result, calls = self.run_deploy(
                    exporter, "create", f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", "missing-existing-link",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                deployments = [call for call in calls if call["args"][:2] == ["deployment", "group"]]
                parameters = deployments[-1]["parameters"]["parameters"]
                self.assertEqual(parameters["zoneNames"]["value"], [])
                self.assertEqual(
                    {link["zoneName"] for link in parameters["vnetLinks"]["value"]},
                    {"existing.example"},
                )

    def test_stale_plan_does_not_duplicate_existing_vnet_links(self):
        result, calls = self.run_deploy("bash", mode="stale-plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1]["parameters"]["parameters"]["zoneNames"]["value"], ["new.example"])
        self.assertEqual(calls[-1]["parameters"]["parameters"]["vnetLinks"]["value"], [])

    def test_lookup_failures_and_unhealthy_links_stop_before_deployment(self):
        for mode in ("missing-vnet", "zone-list-failure", "link-list-failure", "malformed-links", "failed-link"):
            with self.subTest(mode=mode):
                result, calls = self.run_deploy("bash", mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Error:", result.stderr)
                self.assertFalse(any(call["args"][:2] == ["deployment", "group"] for call in calls))

    def test_link_name_collision_creates_resolver_before_removing_other_vnet(self):
        from unittest.mock import patch
        missing, _ = deploy.plan_vnet_links(SUBSCRIPTION, GROUP, set(), ["new.example"], VNET)
        link_name = missing[0]["name"]
        response = json.dumps([{
            "name": link_name, "virtualNetwork": {"id": VNET + "-other"},
            "provisioningState": "Succeeded",
        }, {
            "name": link_name + "-1", "virtualNetwork": {"id": VNET + "-another"},
            "provisioningState": "Succeeded",
        }])
        with patch.object(deploy, "azure_query", return_value=response):
            missing, removals = deploy.plan_vnet_links(SUBSCRIPTION, GROUP, {"new.example"}, [], VNET)
        self.assertEqual(missing, [{"zoneName": "new.example", "name": link_name + "-2"}])
        self.assertEqual({link["name"] for link in removals}, {link_name, link_name + "-1"})

    def test_other_vnet_links_are_previewed_but_only_deleted_after_confirmed_create(self):
        for exporter in self.exporters:
            for action in ("validate", "what-if", "create"):
                with self.subTest(exporter=exporter, action=action):
                    result, calls = self.run_deploy(
                        exporter, action, f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", "other-links",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("2 other VNet links to remove", result.stderr)
                    self.assertIn("existing.example/another-vnet", result.stderr)
                    self.assertNotIn("outside-plan.example", result.stdout + result.stderr)
                    self.assertFalse(any("--zone-name" in call["args"] and
                                         call["args"][call["args"].index("--zone-name") + 1]
                                         == "outside-plan.example" for call in calls))
                    if action == "what-if":
                        self.assertIn("Resources: 2 to delete.", result.stdout)
                        for zone in ("existing.example",):
                            for name, target in (("other-vnet", VNET + "-other"),
                                                 ("another-vnet", VNET + "-another")):
                                self.assertIn(
                                    f"- Delete /subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
                                    f"/providers/Microsoft.Network/privateDnsZones/{zone}"
                                    f"/virtualNetworkLinks/{name}\n"
                                    f"      virtualNetwork.id: {target}",
                                    result.stdout,
                                )
                        self.assertNotIn("/virtualNetworkLinks/arbitrary-existing-name", result.stdout)
                        self.assertGreater(result.stdout.index("Planned VNet link deletions"),
                                           result.stdout.rindex('"Succeeded"'))
                    deletions = [
                        call for call in calls
                        if call["args"][:5] == ["network", "private-dns", "link", "vnet", "delete"]
                    ]
                    if action != "create":
                        self.assertEqual(deletions, [])
                        continue
                    self.assertEqual({
                        (call["args"][call["args"].index("--zone-name") + 1],
                         call["args"][call["args"].index("--name") + 1])
                        for call in deletions
                    }, {
                        (zone, name) for zone in ("existing.example",)
                        for name in ("other-vnet", "another-vnet")
                    })
                    self.assertEqual(calls[-2:], deletions)
                    self.assertTrue(any(call["args"][:3] == ["deployment", "group", "create"]
                                        for call in calls[:-2]))

    def test_cleanup_only_requires_confirmation_and_does_not_deploy(self):
        self.write_plan([row("existing.example", "none", TENANT)])
        for action, confirmation, succeeds, deletion_count in (
            ("validate", "", True, 0),
            ("what-if", "", True, 0),
            ("create", "wrong\n", False, 0),
            ("create", "", False, 0),
            ("create", f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", True, 2),
        ):
            with self.subTest(action=action, confirmation=confirmation):
                result, calls = self.run_deploy("bash", action, confirmation, "cleanup-only")
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertFalse(any(call["args"][:2] == ["deployment", "group"] for call in calls))
                self.assertEqual(len(calls), 5 + deletion_count)
                if action == "what-if":
                    self.assertIn("Resources: 2 to delete.", result.stdout)
                    self.assertIn("/privateDnsZones/existing.example/virtualNetworkLinks/other-vnet",
                                  result.stdout)

    def test_what_if_explicitly_reports_no_link_deletions_for_noop_plan(self):
        self.write_plan([row("existing.example", "none", TENANT)])
        result, calls = self.run_deploy("bash", "what-if", mode="all-linked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resources: 0 to delete.", result.stdout)
        self.assertNotIn("- Delete", result.stdout)
        self.assertEqual(len(calls), 5)

    def test_empty_csv_leaves_existing_zones_and_links_untouched(self):
        self.write_plan([])
        for action in ("validate", "what-if", "create"):
            with self.subTest(action=action):
                result, calls = self.run_deploy("bash", action, mode="other-links")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 4)
                self.assertIn("no changes performed", result.stderr)

    def test_repeated_csv_zone_manages_links_only_when_existing(self):
        self.write_plan([
            row("Existing.Example.", "repeated"), row("missing.example", "none", TENANT),
        ])
        result, calls = self.run_deploy(
            "bash", "create", f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", "missing-existing-link",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(call for call in calls if call["args"][:2] == ["deployment", "group"])
        self.assertEqual(deployment["parameters"]["parameters"]["zoneNames"]["value"], [])
        self.assertEqual(
            {link["zoneName"] for link in deployment["parameters"]["parameters"]["vnetLinks"]["value"]},
            {"existing.example"},
        )
        self.assertEqual(calls[-1]["args"][:5], ["network", "private-dns", "link", "vnet", "delete"])
        self.assertNotIn("missing.example", result.stdout + result.stderr)

    def test_failures_stop_link_cleanup(self):
        from unittest.mock import patch
        removals = [
            {"zoneName": "existing.example", "name": name, "vnetId": VNET + "-other"}
            for name in ("other-vnet", "another-vnet")
        ]
        # Include removals in a failing deployment plan without invoking a live CLI.
        with patch.object(deploy, "plan_vnet_links", return_value=([], removals)), \
                patch.object(deploy, "azure_query", side_effect=[TENANT, "/group", VNET, "existing.example"]), \
                patch("builtins.input", return_value=f"{TENANT}/{SUBSCRIPTION}/{GROUP}"), \
                patch.object(deploy.subprocess, "run",
                             side_effect=subprocess.CalledProcessError(3, ["az"], stderr="denied")):
            self.assertEqual(deploy.main([
                "create", "--tenant-id", TENANT, "--subscription-id", SUBSCRIPTION,
                "--resource-group", GROUP, "--resolver-vnet-id", VNET, "--input-csv", str(self.plan),
            ]), 1)
            self.assertEqual(deploy.azure_query.call_count, 4)
        result, calls = self.run_deploy(
            "bash", "create", f"{TENANT}/{SUBSCRIPTION}/{GROUP}\n", "delete-failure",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot delete link", result.stderr)
        deletions = [call for call in calls
                     if call["args"][:5] == ["network", "private-dns", "link", "vnet", "delete"]]
        self.assertEqual(len(deletions), 1)

    def test_invalid_resolver_resource_ids_are_rejected(self):
        for value in ("resolver", VNET + "/subnets/inbound", VNET.replace(SUBSCRIPTION, "invalid")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    deploy.vnet_id(value)

    def test_invalid_plan_fails_before_azure_calls(self):
        self.write_plan([row("private.example", "delete")])
        for exporter in self.exporters:
            with self.subTest(exporter=exporter):
                result, calls = self.run_deploy(exporter)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported centralTenantOperation", result.stderr)
                self.assertEqual(calls, [])

    def test_plans_above_loop_limit_are_batched_without_losing_zones(self):
        self.write_plan([row(f"zone-{index}.example", "create") for index in range(801)])
        for exporter in self.exporters:
            with self.subTest(exporter=exporter):
                result, calls = self.run_deploy(exporter, "what-if")
                self.assertEqual(result.returncode, 0, result.stderr)
                deployments = [call for call in calls if call["args"][:2] == ["deployment", "group"]]
                self.assertEqual([len(call["parameters"]["parameters"]["zoneNames"]["value"]) for call in deployments],
                                 [400, 400, 1])
                names = [
                    name for call in deployments
                    for name in call["parameters"]["parameters"]["zoneNames"]["value"]
                ]
                self.assertEqual(set(names), {f"zone-{index}.example" for index in range(801)})
                links = [
                    link["zoneName"] for call in deployments
                    for link in call["parameters"]["parameters"]["vnetLinks"]["value"]
                ]
                self.assertEqual(len(links), 801)
                self.assertEqual(set(links), set(names))
                for call in deployments:
                    parameters = call["parameters"]["parameters"]
                    self.assertLessEqual(
                        len(parameters["zoneNames"]["value"]) + len(parameters["vnetLinks"]["value"]), 800,
                    )

    def test_native_command_failure_is_not_reported_as_success(self):
        for exporter in self.exporters:
            with self.subTest(exporter=exporter):
                result, calls = self.run_deploy(exporter, mode="deployment-failure")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Azure CLI command failed", result.stderr)
                self.assertEqual(len(calls), 6)

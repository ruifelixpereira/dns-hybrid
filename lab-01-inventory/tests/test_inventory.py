import contextlib
from copy import deepcopy
import csv
import importlib.util
import io
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
FIXTURES = ROOT / "tests/fixtures"
spec = importlib.util.spec_from_file_location("report", ROOT / "scripts/report_dns_hub_links.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def documents():
    return (
        json.loads((FIXTURES / "private-dns-zones.json").read_text()),
        json.loads((FIXTURES / "virtual-networks.json").read_text()),
    )


def inventory_document():
    dns, networks = documents()
    dns["virtualNetworks"] = networks["virtualNetworks"]
    dns["hubVnetId"] = networks["virtualNetworks"][0]["id"]
    return dns


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.dns, self.networks = documents()
        self.hub = self.networks["virtualNetworks"][0]["id"]

    def rows(self):
        return report.create_rows(self.dns, self.networks, self.hub)

    def add_link(self, vnet_id, name):
        link = deepcopy(self.dns["privateDnsZones"][0]["virtualNetworkLinks"][0])
        link["name"] = name
        link["id"] = link["id"].rsplit("/", 1)[0] + "/" + name
        link["virtualNetwork"] = {"name": vnet_id.rstrip("/").rsplit("/", 1)[1], "id": vnet_id}
        self.dns["privateDnsZones"][0]["virtualNetworkLinks"].append(link)
        return link

    def test_fixture_csv_and_successful_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report with spaces.csv"
            with contextlib.redirect_stderr(io.StringIO()):
                code = report.main([
                    "--private-dns-json", str(FIXTURES / "private-dns-zones.json"),
                    "--vnets-json", str(FIXTURES / "virtual-networks.json"),
                    "--hub-vnet-id", self.hub, "--output-csv", str(output),
                ])
            self.assertEqual(code, 0)
            with output.open(newline="") as stream, (FIXTURES / "expected.csv").open(newline="") as expected:
                self.assertEqual(list(csv.reader(stream)), list(csv.reader(expected)))

    def test_tenant_id_comes_from_inventory_including_external_links(self):
        tenant = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.dns["tenantId"] = tenant
        self.networks["tenantId"] = tenant
        self.add_link(
            self.networks["virtualNetworks"][0]["peerings"][1]["remoteVirtualNetwork"]["id"],
            "external",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.csv"
            report.write_csv(self.rows(), output)
            with output.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertEqual({row["tenantId"] for row in rows}, {tenant})

    def test_combined_inventory_cli_matches_fixture_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = Path(directory) / "tenant inventory.json"
            inventory_path.write_text(json.dumps(inventory_document()))
            output = Path(directory) / "report.csv"
            with contextlib.redirect_stderr(io.StringIO()):
                code = report.main([
                    "--inventory-json", str(inventory_path),
                    "--hub-vnet-id", self.hub, "--output-csv", str(output),
                ])
            self.assertEqual(code, 0)
            with output.open(newline="") as stream, (FIXTURES / "expected.csv").open(newline="") as expected:
                self.assertEqual(list(csv.reader(stream)), list(csv.reader(expected)))

    def test_invalid_combined_inventory_preserves_output_and_input(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = Path(directory) / "inventory.json"
            output = Path(directory) / "report.csv"
            output.write_text("existing report")
            for collection in ("privateDnsZones", "virtualNetworks"):
                document = inventory_document()
                del document[collection]
                original = json.dumps(document)
                inventory_path.write_text(original)
                for destination in (output, inventory_path):
                    with self.subTest(collection=collection, destination=destination):
                        error = io.StringIO()
                        with contextlib.redirect_stderr(error):
                            code = report.main([
                                "--inventory-json", str(inventory_path),
                                "--hub-vnet-id", self.hub, "--output-csv", str(destination),
                            ])
                        self.assertEqual(code, 1)
                        self.assertIn("Error:", error.getvalue())
                        self.assertEqual(output.read_text(), "existing report")
                        self.assertEqual(inventory_path.read_text(), original)

    def test_rejects_mixed_or_incomplete_inventory_arguments(self):
        for inputs in (
            ["--inventory-json", "combined.json", "--private-dns-json", "dns.json"],
            ["--inventory-json", "combined.json", "--vnets-json", "vnets.json"],
            ["--private-dns-json", "dns.json"],
        ):
            with self.subTest(inputs=inputs), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    report.main(inputs + ["--hub-vnet-id", self.hub, "--output-csv", "report.csv"])
                self.assertEqual(error.exception.code, 2)

    def test_direct_either_endpoint_external_and_not_second_hop(self):
        vnets = self.networks["virtualNetworks"]
        self.add_link(vnets[2]["id"], "incoming")
        self.add_link(vnets[0]["peerings"][1]["remoteVirtualNetwork"]["id"], "external")
        self.add_link(vnets[1]["peerings"][0]["remoteVirtualNetwork"]["id"], "second-hop")
        self.assertEqual(
            {row["virtualNetworkLinkName"] for row in self.rows()},
            {"hub-link", "spoke-link", "incoming", "external"},
        )

    def test_case_trailing_slash_duplicate_evidence_and_names(self):
        self.hub = self.hub.upper() + "/"
        zone = self.dns["privateDnsZones"][0]
        link = zone["virtualNetworkLinks"][0]
        link["virtualNetwork"]["id"] = self.hub
        zone["virtualNetworkLinks"].append(deepcopy(link))
        self.networks["virtualNetworks"][0]["peerings"].append(
            deepcopy(self.networks["virtualNetworks"][0]["peerings"][0])
        )
        other_zone = self.dns["privateDnsZones"][1]
        other_link = deepcopy(link)
        other_link["id"] = other_zone["id"] + "/virtualNetworkLinks/hub-link"
        other_zone["virtualNetworkLinks"].append(other_link)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["privateDnsZoneId"] for r in rows}), 2)

    def test_csv_escaping_and_header_only_output(self):
        self.dns["privateDnsZones"][0]["virtualNetworkLinks"][0]["name"] = 'a,"quoted"\nlink'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.csv"
            report.write_csv(self.rows(), output)
            with output.open(newline="") as stream:
                self.assertEqual(next(csv.DictReader(stream))["virtualNetworkLinkName"], 'a,"quoted"\nlink')
            self.dns["privateDnsZones"] = []
            report.write_csv(self.rows(), output)
            with output.open(newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [report.COLUMNS])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["report.csv"])

    def test_missing_hub_and_conflicting_duplicates(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            report.create_rows(self.dns, self.networks, self.hub + "-missing")
        duplicate = deepcopy(self.dns["privateDnsZones"][0]["virtualNetworkLinks"][0])
        duplicate["virtualNetwork"] = self.dns["privateDnsZones"][0]["virtualNetworkLinks"][1]["virtualNetwork"]
        self.dns["privateDnsZones"][0]["virtualNetworkLinks"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.rows()

    def test_rejects_mismatched_metadata(self):
        for key, value in (
            ("inventoryId", "different"), ("tenantId", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            ("collectionCompletedAt", "2026-11-18T12:02:00Z"), ("schemaVersion", 2),
            ("subscriptions", [self.networks["subscriptions"][0]]),
        ):
            with self.subTest(key=key):
                networks = deepcopy(self.networks)
                networks[key] = value
                with self.assertRaises(ValueError):
                    report.create_rows(self.dns, networks, self.hub)

    def test_rejects_invalid_shapes_and_types(self):
        mutations = (
            lambda doc: doc.update(schemaVersion=True),
            lambda doc: doc.update(privateDnsZones={}),
            lambda doc: doc.update(collectionStartedAt="yesterday"),
            lambda doc: doc["privateDnsZones"][0].pop("aRecordSets"),
            lambda doc: doc["privateDnsZones"][0]["aRecordSets"][0].update(ttl=True),
            lambda doc: doc["privateDnsZones"][0]["aRecordSets"][0].update(ipv4Addresses=["not-an-ip"]),
            lambda doc: doc["privateDnsZones"][0]["virtualNetworkLinks"][0].update(registrationEnabled="false"),
            lambda doc: doc["privateDnsZones"][0]["virtualNetworkLinks"][0]["virtualNetwork"].update(id="hub"),
            lambda doc: doc["privateDnsZones"][0].update(subscriptionId="99999999-9999-9999-9999-999999999999"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                dns = deepcopy(self.dns)
                mutation(dns)
                with self.assertRaises(ValueError):
                    report.create_rows(dns, self.networks, self.hub)

    def test_invalid_cli_input_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            dns_path = directory / "dns.json"
            output = directory / "report.csv"
            output.write_text("existing report")
            for malformed in ('{"broken"', '{"schemaVersion":1,"schemaVersion":1}', '[]'):
                dns_path.write_text(malformed)
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    code = report.main([
                        "--private-dns-json", str(dns_path),
                        "--vnets-json", str(FIXTURES / "virtual-networks.json"),
                        "--hub-vnet-id", self.hub, "--output-csv", str(output),
                    ])
                self.assertEqual(code, 1)
                self.assertIn("Error:", error.getvalue())
                self.assertEqual(output.read_text(), "existing report")


class BatchReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "inventory"
        self.directory.mkdir()

    def write_inventory(self, tenant, hub=None):
        document = inventory_document()
        document["tenantId"] = tenant
        if hub is not None:
            document["hubVnetId"] = hub
        source = self.directory / f"{tenant}-inventory.json"
        source.write_text(json.dumps(document))
        return source

    def run_script(self, directory=None):
        command = [sys.executable, str(ROOT / "scripts/report_all_dns_hub_links.py")]
        if directory is not None:
            command += ["--inventory-directory", str(directory)]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=30)

    def test_default_directory_generates_each_tenant_report_using_its_hub(self):
        vnets = documents()[1]["virtualNetworks"]
        cases = (
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", vnets[0]["id"]),
            ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", vnets[1]["id"]),
        )
        for tenant, hub in cases:
            self.write_inventory(tenant, hub)
        (self.directory / "unrelated.json").write_text("not an inventory")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for tenant, hub in cases:
            output = self.directory / f"{tenant}-hub-dns-links.csv"
            with output.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["tenantId"] for row in rows}, {tenant})
            self.assertEqual({row["hubVnetId"] for row in rows}, {hub})
        self.assertEqual(len(list(self.directory.iterdir())), 5)

    def test_custom_directory_with_spaces_skips_missing_or_null_hubs(self):
        self.directory = self.root / "tenant inventories"
        self.directory.mkdir()
        for tenant, remove in (
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", True),
            ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", False),
        ):
            source = self.write_inventory(tenant)
            document = json.loads(source.read_text())
            if remove:
                del document["hubVnetId"]
            else:
                document["hubVnetId"] = None
            source.write_text(json.dumps(document))
        result = self.run_script(self.directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("Skipping "), 2)
        self.assertFalse(list(self.directory.glob("*.csv")))

    def test_invalid_inventory_preserves_existing_csv(self):
        tenant = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        source = self.write_inventory(tenant)
        output = self.directory / f"{tenant}-hub-dns-links.csv"
        invalid = inventory_document()
        invalid["hubVnetId"] = "/invalid/hub"
        for content in ('{"broken"', json.dumps(invalid)):
            with self.subTest(content=content):
                source.write_text(content)
                output.write_text("existing report")
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Error:", result.stderr)
                self.assertEqual(output.read_text(), "existing report")
                self.assertFalse(list(self.directory.glob(".*.tmp")))

    def test_missing_directory_or_no_inventories_is_an_error(self):
        for directory in (self.directory, self.root / "missing"):
            with self.subTest(directory=directory):
                result = self.run_script(directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Error:", result.stderr)

    def test_duplicate_tenant_inventories_are_rejected(self):
        source = self.write_inventory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        (self.directory / "copy-inventory.json").write_text(source.read_text())
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate inventory for tenant", result.stderr)


class ExporterTests(unittest.TestCase):
    exporters = ("bash", "powershell")
    tenant_id = documents()[0]["tenantId"]
    inventory_filename = f"{tenant_id}-inventory.json"

    def run_export(self, exporter, directory, mode=""):
        if exporter == "powershell":
            pwsh = os.environ.get("PWSH") or shutil.which("pwsh")
            if not pwsh:
                self.skipTest("PowerShell 7.4+ is not installed; set PWSH to its executable")
            command = [pwsh, "-NoLogo", "-NoProfile", "-File", str(ROOT / "scripts/Export-Inventory.ps1"),
                       "-TenantId", self.tenant_id, "-OutputDirectory"]
        else:
            if not shutil.which("jq"):
                self.skipTest("jq is not installed")
            command = ["bash", str(ROOT / "scripts/export-inventory.sh"),
                       "--tenant-id", self.tenant_id, "--output-directory"]
        mock_bin = directory / "mock bin"
        mock_bin.mkdir(exist_ok=True)
        az = mock_bin / "az"
        az.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
            shlex.quote(str(ROOT / "tests/mock_az.py")) + ' "$@"\n'
        )
        az.chmod(0o700)
        output = directory / "output with spaces"
        log = directory / "calls.jsonl"
        result = subprocess.run(
            command + [str(output)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"],
                 "MOCK_LOG": str(log), "MOCK_MODE": mode, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, output, calls

    def test_bash_export(self):
        self.check_export("bash")

    def test_powershell_export(self):
        self.check_export("powershell")

    def check_export(self, exporter):
        with tempfile.TemporaryDirectory() as directory:
            result, output, calls = self.run_export(exporter, Path(directory))
            self.assertEqual(result.returncode, 0, result.stderr)
            inventory_path = output / self.inventory_filename
            inventory = json.loads(inventory_path.read_text())
            report.validate_documents(inventory, inventory)
            actual = deepcopy(inventory)
            expected = inventory_document()
            for key in ("inventoryId", "collectionStartedAt", "collectionCompletedAt"):
                actual.pop(key)
                expected.pop(key)
            self.assertEqual(self.sort_resources(actual), self.sort_resources(expected))
            for collection in ("privateDnsZones", "virtualNetworks", "/A", "virtualNetworkLinks", "virtualNetworkPeerings"):
                with self.subTest(paginated_collection=collection):
                    self.assertTrue(any(
                        len(call) > 4 and call[0] == "rest" and
                        call[4].split("?", 1)[0].endswith(collection) and "page=1" in call[4]
                        for call in calls
                    ))
            self.assertEqual(
                sorted(p.name for p in output.iterdir()),
                [self.inventory_filename],
            )
            hub = inventory["virtualNetworks"][0]["id"]
            csv_path = Path(directory) / "report.csv"
            with contextlib.redirect_stderr(io.StringIO()):
                code = report.main([
                    "--inventory-json", str(inventory_path),
                    "--hub-vnet-id", hub, "--output-csv", str(csv_path),
                ])
            self.assertEqual(code, 0)
            with csv_path.open(newline="") as stream, (FIXTURES / "expected.csv").open(newline="") as expected_csv:
                self.assertEqual(list(csv.reader(stream)), list(csv.reader(expected_csv)))

    @classmethod
    def sort_resources(cls, value):
        if isinstance(value, dict):
            return {key: cls.sort_resources(item) for key, item in value.items()}
        if isinstance(value, list):
            result = [cls.sort_resources(item) for item in value]
            if result and isinstance(result[0], dict) and "id" in result[0]:
                result.sort(key=lambda item: item["id"].lower())
            return result
        return value

    def test_empty_inventories(self):
        for exporter in self.exporters:
            with self.subTest(exporter=exporter), tempfile.TemporaryDirectory() as directory:
                result, output, _ = self.run_export(exporter, Path(directory), "empty")
                self.assertEqual(result.returncode, 0, result.stderr)
                inventory = json.loads((output / self.inventory_filename).read_text())
                report.validate_documents(inventory, inventory)
                self.assertEqual(inventory["privateDnsZones"], [])
                self.assertEqual(inventory["virtualNetworks"], [])
                self.assertIsNone(inventory["hubVnetId"])
                self.assertEqual([path.name for path in output.iterdir()], [self.inventory_filename])

    def test_hub_selection_across_subscriptions_and_tie_breakers(self):
        vnets = documents()[1]["virtualNetworks"]
        cases = (
            ("hub-other-subscription", vnets[2]["id"]),
            ("hub-dns-link-tie", vnets[1]["id"]),
            ("hub-id-tie", vnets[0]["id"]),
            ("hub-zero-peerings", vnets[0]["id"]),
        )
        for exporter in self.exporters:
            for mode, expected_hub in cases:
                with self.subTest(exporter=exporter, mode=mode), tempfile.TemporaryDirectory() as directory:
                    result, output, _ = self.run_export(exporter, Path(directory), mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    inventory = json.loads((output / self.inventory_filename).read_text())
                    report.validate_documents(inventory, inventory)
                    self.assertEqual(inventory["hubVnetId"], expected_hub)

    def test_failures_preserve_outputs_and_clean_staging(self):
        for exporter in self.exporters:
            for mode in ("subscription-failure", "child-failure", "malformed", "cycle",
                         "foreign-next-link", "no-subscriptions", "missing-child-id"):
                with self.subTest(exporter=exporter, mode=mode), tempfile.TemporaryDirectory() as directory:
                    directory = Path(directory)
                    output = directory / "output with spaces"
                    output.mkdir()
                    (output / self.inventory_filename).write_text("previous valid data")
                    result, _, calls = self.run_export(exporter, directory, mode)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Error", result.stderr)
                    self.assertEqual(len(list(output.iterdir())), 1)
                    for path in output.iterdir():
                        self.assertEqual(path.read_text(), "previous valid data")
                    if mode == "child-failure":
                        self.assertIn("zone", result.stderr)
                        self.assertEqual(sum("/virtualNetworkLinks?" in " ".join(c) for c in calls), 1)

    def test_transient_retry_succeeds_and_is_bounded(self):
        for exporter in self.exporters:
            for mode in ("transient-once", "transient-always"):
                with self.subTest(exporter=exporter, mode=mode), tempfile.TemporaryDirectory() as directory:
                    result, output, calls = self.run_export(exporter, Path(directory), mode)
                    if mode == "transient-once":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue((output / self.inventory_filename).exists())
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(sum("/privateDnsZones?" in " ".join(c) for c in calls), 3)
                        self.assertEqual(list(output.iterdir()), [])

    def test_export_preserves_legacy_and_other_tenant_files(self):
        for exporter in self.exporters:
            with self.subTest(exporter=exporter), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                output = directory / "output with spaces"
                output.mkdir()
                preserved = [
                    "private-dns-zones.json", "virtual-networks.json",
                    f"{self.tenant_id}-private-dns-zones.json",
                    f"{self.tenant_id}-virtual-networks.json",
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb-inventory.json",
                ]
                for name in preserved:
                    (output / name).write_text("existing inventory")
                result, _, _ = self.run_export(exporter, directory, "empty")
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in preserved:
                    self.assertEqual((output / name).read_text(), "existing inventory")
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    sorted(preserved + [self.inventory_filename]),
                )


if __name__ == "__main__":
    unittest.main()

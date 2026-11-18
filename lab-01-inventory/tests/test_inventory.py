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


class ExporterTests(unittest.TestCase):
    exporters = ("bash", "powershell")

    def run_export(self, exporter, directory, mode=""):
        if exporter == "powershell":
            pwsh = os.environ.get("PWSH") or shutil.which("pwsh")
            if not pwsh:
                self.skipTest("PowerShell 7.4+ is not installed; set PWSH to its executable")
            command = [pwsh, "-NoLogo", "-NoProfile", "-File", str(ROOT / "scripts/Export-Inventory.ps1"),
                       "-TenantId", documents()[0]["tenantId"], "-OutputDirectory"]
        else:
            if not shutil.which("jq"):
                self.skipTest("jq is not installed")
            command = ["bash", str(ROOT / "scripts/export-inventory.sh"),
                       "--tenant-id", documents()[0]["tenantId"], "--output-directory"]
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
            dns = json.loads((output / "private-dns-zones.json").read_text())
            networks = json.loads((output / "virtual-networks.json").read_text())
            report.validate_documents(dns, networks)
            for actual, expected in zip((dns, networks), documents()):
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
                ["private-dns-zones.json", "virtual-networks.json"],
            )
            hub = networks["virtualNetworks"][0]["id"]
            self.assertEqual(len(report.create_rows(
                json.loads((output / "private-dns-zones.json").read_text()),
                json.loads((output / "virtual-networks.json").read_text()), hub,
            )), 2)

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
                dns = json.loads((output / "private-dns-zones.json").read_text())
                networks = json.loads((output / "virtual-networks.json").read_text())
                report.validate_documents(dns, networks)
                self.assertEqual(dns["privateDnsZones"], [])
                self.assertEqual(networks["virtualNetworks"], [])

    def test_failures_preserve_outputs_and_clean_staging(self):
        for exporter in self.exporters:
            for mode in ("subscription-failure", "child-failure", "malformed", "cycle",
                         "foreign-next-link", "no-subscriptions", "missing-child-id"):
                with self.subTest(exporter=exporter, mode=mode), tempfile.TemporaryDirectory() as directory:
                    directory = Path(directory)
                    output = directory / "output with spaces"
                    output.mkdir()
                    for name in ("private-dns-zones.json", "virtual-networks.json"):
                        (output / name).write_text("previous valid data")
                    result, _, calls = self.run_export(exporter, directory, mode)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Error", result.stderr)
                    self.assertEqual(len(list(output.iterdir())), 2)
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
                        self.assertTrue((output / "private-dns-zones.json").exists())
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(sum("/privateDnsZones?" in " ".join(c) for c in calls), 3)
                        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/consolidate-dns-links.sh"
FIXTURES = ROOT / "tests/fixtures"
SOURCE_COLUMNS = [
    "privateDnsZoneName", "tenantId", "privateDnsZoneId",
    "privateDnsZoneSubscriptionId", "privateDnsZoneResourceGroup",
]
COLUMNS = SOURCE_COLUMNS + ["centralTenantOperation", "originalTenantOperation"]
CENTRAL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
THIRD = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()

    @property
    def output(self):
        return self.inventory / "hub-dns-zones.csv"

    def write_inventory(self, tenant, names, hub=True):
        document = json.loads((FIXTURES / "private-dns-zones.json").read_text())
        networks = json.loads((FIXTURES / "virtual-networks.json").read_text())
        template = document["privateDnsZones"][0]
        document["tenantId"] = tenant
        document["virtualNetworks"] = networks["virtualNetworks"]
        document["hubVnetId"] = networks["virtualNetworks"][0]["id"] if hub else None
        document["privateDnsZones"] = []
        rows = []
        for index, name in enumerate(names):
            zone = deepcopy(template)
            zone["name"] = name
            zone["resourceGroup"] = f"dns-{index}"
            zone["id"] = (
                f"/subscriptions/{zone['subscriptionId']}/resourceGroups/{zone['resourceGroup']}"
                f"/providers/Microsoft.Network/privateDnsZones/{name}"
            )
            zone["aRecordSets"] = []
            for link in zone["virtualNetworkLinks"]:
                link["id"] = zone["id"] + "/virtualNetworkLinks/" + link["name"]
            document["privateDnsZones"].append(zone)
            rows.append([name, tenant, zone["id"], zone["subscriptionId"], zone["resourceGroup"]])
        (self.inventory / f"{tenant}-inventory.json").write_text(json.dumps(document))
        return rows

    def run_script(self, central=CENTRAL, directory=None):
        command = ["bash", str(SCRIPT), "--central-tenant-id", central]
        if directory is not None:
            command += ["--inventory-directory", str(directory)]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=30)

    def read_output(self):
        with self.output.open(newline="") as stream:
            return list(csv.reader(stream))

    def test_one_step_generates_reports_and_consolidates_default_folder(self):
        existing = self.write_inventory(CENTRAL, ["private.example"])[0]
        shared, missing = self.write_inventory(OTHER, ["private.example", "new.example"])
        old_report = self.inventory / f"{OTHER}-hub-dns-links.csv"
        old_report.write_text("stale report")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_output(), [
            COLUMNS, existing + ["none", "none"],
            shared + ["repeated", "delete"], missing + ["create", "delete"],
        ])
        for tenant in (CENTRAL, OTHER):
            with (self.inventory / f"{tenant}-hub-dns-links.csv").open(newline="") as stream:
                self.assertGreater(len(list(csv.reader(stream))), 1)
        self.assertEqual(len(list(self.inventory.iterdir())), 5)

    def test_central_domains_are_considered_even_when_central_inventory_is_last(self):
        shared, missing = self.write_inventory(CENTRAL, ["PRIVATE.EXAMPLE.", "new.example"])
        existing, central_only = self.write_inventory(OTHER, ["private.example", "central.example"])
        result = self.run_script(central=OTHER.upper())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_output(), [
            COLUMNS, shared + ["repeated", "delete"], missing + ["create", "delete"],
            existing + ["none", "none"], central_only + ["none", "none"],
        ])

    def test_missing_domain_is_created_once_and_csv_escaping_is_preserved(self):
        name = 'zone,"quoted"\nname'
        self.write_inventory(CENTRAL, [])
        first, distinct = self.write_inventory(OTHER, [name, name])
        second = self.write_inventory(THIRD, [name])[0]
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_output(), [
            COLUMNS, first + ["create", "delete"], distinct + ["repeated", "delete"],
            second + ["repeated", "delete"],
        ])

    def test_custom_inventory_directory_with_spaces(self):
        self.inventory = self.root / "tenant inventories"
        self.inventory.mkdir()
        existing = self.write_inventory(CENTRAL, ["private.example"])[0]
        result = self.run_script(directory=self.inventory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_output(), [COLUMNS, existing + ["none", "none"]])
        self.assertFalse((self.root / "inventory/hub-dns-zones.csv").exists())

    def test_skipped_and_orphaned_stale_csvs_are_not_consolidated(self):
        self.write_inventory(CENTRAL, [])
        self.write_inventory(OTHER, ["skipped.example"], hub=False)
        for tenant in (OTHER, THIRD):
            (self.inventory / f"{tenant}-hub-dns-links.csv").write_text("stale invalid csv")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipping ", result.stderr)
        self.assertEqual(self.read_output(), [COLUMNS])
        for tenant in (OTHER, THIRD):
            self.assertEqual(
                (self.inventory / f"{tenant}-hub-dns-links.csv").read_text(), "stale invalid csv",
            )

    def test_batch_failure_preserves_existing_final_csv(self):
        self.write_inventory(CENTRAL, ["private.example"])
        self.write_inventory(OTHER, ["other.example"])
        source = self.inventory / f"{OTHER}-inventory.json"
        invalid = json.loads(source.read_text())
        invalid["hubVnetId"] = "/invalid/hub"
        for content in ('{"broken"', json.dumps(invalid)):
            with self.subTest(content=content):
                source.write_text(content)
                self.output.write_text("existing final plan")
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Error:", result.stderr)
                self.assertEqual(self.output.read_text(), "existing final plan")
                self.assertFalse(list(self.inventory.glob(".hub-dns-zones.csv.*")))

    def test_invalid_central_tenant_fails_before_report_generation(self):
        self.write_inventory(CENTRAL, ["private.example"])
        self.output.write_text("existing final plan")
        result = self.run_script(central="not-a-guid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected a tenant GUID", result.stderr)
        self.assertEqual(self.output.read_text(), "existing final plan")
        self.assertFalse(list(self.inventory.glob("*-hub-dns-links.csv")))

    def test_no_inventories_preserves_existing_final_csv(self):
        self.output.write_text("existing final plan")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tenant inventory files", result.stderr)
        self.assertEqual(self.output.read_text(), "existing final plan")

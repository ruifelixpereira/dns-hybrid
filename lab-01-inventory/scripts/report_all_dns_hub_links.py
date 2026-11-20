#!/usr/bin/env python3
"""Generate a hub DNS link CSV for each tenant inventory containing a hub VNet ID."""

import argparse
from pathlib import Path
import sys

import report_dns_hub_links as report


def generate_reports(directory):
    if not directory.is_dir():
        raise ValueError(f"Inventory directory does not exist: {directory}")
    sources = sorted(directory.glob("*-inventory.json"), key=lambda path: path.name.casefold())
    if not sources:
        raise ValueError(f"No tenant inventory files found in {directory}")
    tenants = set()
    generated = []
    for source in sources:
        document = report.load_json(source)
        report.required(document, ("tenantId",), str(source))
        tenant = report.guid(document["tenantId"], f"{source}.tenantId")
        if tenant in tenants:
            raise ValueError(f"{source}: duplicate inventory for tenant {tenant}")
        tenants.add(tenant)
        hub = document.get("hubVnetId")
        if hub is None:
            print(f"Skipping {source}: hubVnetId is missing or null", file=sys.stderr)
            continue
        report.text(hub, f"{source}.hubVnetId")
        destination = directory / f"{tenant}-hub-dns-links.csv"
        if report.main([
            "--inventory-json", str(source),
            "--hub-vnet-id", hub,
            "--output-csv", str(destination),
        ]) != 0:
            raise ValueError(f"{source}: hub DNS link report generation failed")
        generated.append(destination)
    print(f"Generated {len(generated)} tenant hub DNS link reports in {directory}", file=sys.stderr)
    return generated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-directory", type=Path, default=Path("./inventory"))
    args = parser.parse_args(argv)
    try:
        generate_reports(args.inventory_directory)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

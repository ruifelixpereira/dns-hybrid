#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

command -v python3 >/dev/null || { printf 'Error: python3 is required\n' >&2; exit 1; }
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

python3 - "$script_directory" "$@" <<'PY'
import argparse
import csv
import os
from pathlib import Path
import sys
import tempfile
from uuid import UUID

sys.path.insert(0, sys.argv[1])
import report_all_dns_hub_links as batch

parser = argparse.ArgumentParser(description="Generate tenant hub-link reports and consolidate a central-tenant plan")
parser.add_argument("--central-tenant-id", required=True)
parser.add_argument("--inventory-directory", type=Path, default=Path("./inventory"))
args = parser.parse_args(sys.argv[2:])

source_columns = [
    "privateDnsZoneName", "tenantId", "privateDnsZoneId",
    "privateDnsZoneSubscriptionId", "privateDnsZoneResourceGroup",
]
columns = source_columns + ["centralTenantOperation", "originalTenantOperation"]
output = args.inventory_directory / "hub-dns-zones.csv"
staged = None


def tenant_guid(value, context):
    try:
        normalized = str(UUID(value))
    except ValueError as error:
        raise ValueError(f"{context}: expected a tenant GUID") from error
    if normalized != value.casefold():
        raise ValueError(f"{context}: expected a tenant GUID")
    return normalized


def domain_name(value):
    return value.rstrip(".").casefold()


try:
    central_tenant = tenant_guid(args.central_tenant_id, "--central-tenant-id")
    inputs = batch.generate_reports(args.inventory_directory)
    for source in inputs:
        if output.resolve() == source.resolve() or (
            output.exists() and output.samefile(source)
        ):
            raise ValueError("Output must not overwrite an input file")

    zones = {}
    for source in inputs:
        with source.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)) or not set(source_columns).issubset(headers):
                raise ValueError(f"{source}: expected unique headers including {','.join(source_columns)}")
            for row in reader:
                context = f"{source}:{reader.line_num}"
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{context}: row length does not match headers")
                if not all(row[column].strip() for column in source_columns):
                    raise ValueError(f"{context}: missing required zone metadata")
                tenant = tenant_guid(row["tenantId"], context)
                zone = {column: row[column] for column in source_columns}
                key = (tenant, zone["privateDnsZoneId"].rstrip("/").casefold())
                if key in zones:
                    existing = zones[key]
                    if (
                        domain_name(existing["privateDnsZoneName"]) != domain_name(zone["privateDnsZoneName"])
                        or existing["privateDnsZoneSubscriptionId"].casefold() != zone["privateDnsZoneSubscriptionId"].casefold()
                        or existing["privateDnsZoneResourceGroup"].casefold() != zone["privateDnsZoneResourceGroup"].casefold()
                    ):
                        raise ValueError(f"{context}: conflicting duplicate zone metadata")
                    continue
                zones[key] = zone

    central_domains = {
        domain_name(zone["privateDnsZoneName"])
        for (tenant, _), zone in zones.items() if tenant == central_tenant
    }
    for (tenant, _), zone in zones.items():
        domain = domain_name(zone["privateDnsZoneName"])
        if tenant == central_tenant:
            zone["centralTenantOperation"] = "none"
            zone["originalTenantOperation"] = "none"
        else:
            zone["centralTenantOperation"] = "repeated" if domain in central_domains else "create"
            zone["originalTenantOperation"] = "delete"
            central_domains.add(domain)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=output.parent,
        prefix=f".{output.name}.", delete=False,
    ) as stream:
        staged = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(zones.values())
    os.replace(staged, output)
    print(f"Wrote {len(zones)} consolidated zone rows to {output}", file=sys.stderr)
except (OSError, UnicodeError, csv.Error, ValueError, TypeError, RecursionError) as error:
    print(f"Error: {error}", file=sys.stderr)
    sys.exit(1)
finally:
    if staged is not None and staged.exists():
        staged.unlink()
PY

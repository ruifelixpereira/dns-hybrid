#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
    printf 'Usage: bash %s --tenant-id <GUID> [--output-directory <path>]\n' "$0"
}
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

tenant_id='' output_directory='./inventory' stage='' context='initialization'
while (($#)); do
    case "$1" in
        --tenant-id|--output-directory)
            (($# >= 2)) && [[ -n "$2" ]] || die "Missing value for $1"
            if [[ "$1" == '--tenant-id' ]]; then tenant_id=$2; else output_directory=$2; fi
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
done
[[ "$tenant_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] ||
    die '--tenant-id must be a tenant GUID'
for dependency in az jq mktemp date; do
    command -v "$dependency" >/dev/null || die "Missing dependency: $dependency"
done
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -r "$script_directory/inventory.jq" ]] || die 'Missing inventory.jq helper'
cleanup() { if [[ -n "$stage" ]]; then rm -rf -- "$stage"; fi; }
trap cleanup EXIT
trap 'printf "Error during %s (line %s)\n" "$context" "$LINENO" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p -- "$output_directory"
output_directory=$(cd -- "$output_directory" && pwd)
for file in private-dns-zones.json virtual-networks.json; do
    [[ ! -d "$output_directory/$file" ]] || die "Output is a directory: $output_directory/$file"
done
stage=$(mktemp -d "$output_directory/.inventory.XXXXXXXX")
started=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
inventory_id="${started}-$$-${stage##*.}"

invoke_az() {
    local destination=$1 attempt
    shift
    for attempt in 1 2 3; do
        if az "$@" --only-show-errors --output json >"$destination" 2>"$stage/az-error"; then
            jq -e -s 'length == 1' "$destination" >/dev/null ||
                die "Invalid Azure CLI JSON during $context"
            return
        fi
        if ((attempt < 3)) &&
            grep -Eiq '(^|[^[:alnum:]])(429|500|502|503|504)([^[:alnum:]]|$)|TooManyRequests|ServiceUnavailable|InternalServerError|GatewayTimeout' "$stage/az-error"; then
            printf 'Retrying %s (attempt %s/3)\n' "$context" "$((attempt + 1))" >&2
            sleep "$attempt"
        else
            printf 'Error: Azure CLI failed during %s:\n' "$context" >&2
            cat "$stage/az-error" >&2
            exit 1
        fi
    done
}

list_all() {
    local url=$1 subscription=$2 destination=$3
    local -A visited=()
    : >"$stage/items.ndjson"
    while [[ -n "$url" ]]; do
        [[ "$url" == "$endpoint/subscriptions/$subscription/"* ]] ||
            die "Unexpected pagination URL during $context: $url"
        [[ ! ${visited[$url]+present} ]] || die "Pagination cycle during $context"
        visited["$url"]=1
        invoke_az "$stage/page.json" rest --method get --url "$url" --subscription "$subscription"
        jq -e 'type == "object" and (.value | type == "array") and
            ((.nextLink == null) or (.nextLink | type == "string"))' "$stage/page.json" >/dev/null ||
            die "Invalid list response during $context"
        jq -c '.value[]' "$stage/page.json" >>"$stage/items.ndjson"
        url=$(jq -r '.nextLink // empty' "$stage/page.json")
    done
    jq -s '.' "$stage/items.ndjson" >"$destination"
}

resource_path() { jq -rn --arg id "$1" '$id | split("/") | map(@uri) | join("/")'; }
normalize() { jq -e -L "$script_directory" --arg subscriptionId "$subscription" "$@"; }

context='subscription discovery'
invoke_az "$stage/accounts.json" account list --all --refresh
jq -e 'type == "array" and all(.[];
    (.name | type == "string" and length > 0) and
    (.id | type == "string" and test("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")) and
    (.tenantId | type == "string" and length > 0) and
    (.state | type == "string" and length > 0))' "$stage/accounts.json" >/dev/null ||
    die 'Invalid subscription discovery response'
jq --arg tenant "$tenant_id" '
    [.[] | select((.tenantId | ascii_downcase) == ($tenant | ascii_downcase))] |
    {subscriptions: [ .[] | select(.state == "Enabled") | {name,id,state}],
     excludedSubscriptions: [ .[] | select(.state != "Enabled") |
       {name,id,state,reason: ("Subscription state is " + .state)}]} |
    .subscriptions |= sort_by(.id | ascii_downcase) |
    .excludedSubscriptions |= sort_by(.id | ascii_downcase)
' "$stage/accounts.json" >"$stage/coverage.json"
[[ $(jq '.subscriptions | length' "$stage/coverage.json") -gt 0 ]] ||
    die "No enabled visible subscriptions in tenant $tenant_id; sign in with az login --tenant $tenant_id"
context='reading configured cloud'
invoke_az "$stage/cloud.json" cloud show
endpoint=$(jq -er '.endpoints.resourceManager | strings | select(startswith("https://")) | sub("/+$"; "")' "$stage/cloud.json")
: >"$stage/zones.ndjson"
: >"$stage/vnets.ndjson"
mapfile -t subscriptions < <(jq -r '.subscriptions[].id' "$stage/coverage.json")
for subscription in "${subscriptions[@]}"; do
    printf 'Collecting subscription %s\n' "$subscription" >&2
    context="private DNS zones in subscription $subscription"
    list_all "$endpoint/subscriptions/$subscription/providers/Microsoft.Network/privateDnsZones?api-version=2018-09-01" \
        "$subscription" "$stage/zones.json"
    jq -c '.[]' "$stage/zones.json" >"$stage/zone-list.ndjson"
    while IFS= read -r zone; do
        id=$(jq -er '.id | strings | select(length > 0)' <<<"$zone")
        context="zone $id in subscription $subscription"
        printf '%s\n' "$zone" | normalize 'include "inventory"; resource("privateDnsZones")' >"$stage/zone.json"
        path=$(resource_path "$id")
        list_all "$endpoint$path/A?api-version=2018-09-01" "$subscription" "$stage/records.json"
        list_all "$endpoint$path/virtualNetworkLinks?api-version=2018-09-01" "$subscription" "$stage/links.json"
        normalize --slurpfile records "$stage/records.json" --slurpfile links "$stage/links.json" \
            'include "inventory"; . + {aRecordSets: ($records[0] | records), virtualNetworkLinks: ($links[0] | links)}' \
            "$stage/zone.json" >>"$stage/zones.ndjson"
    done <"$stage/zone-list.ndjson"
    context="VNets in subscription $subscription"
    list_all "$endpoint/subscriptions/$subscription/providers/Microsoft.Network/virtualNetworks?api-version=2026-03-01" \
        "$subscription" "$stage/vnets.json"
    jq -c '.[]' "$stage/vnets.json" >"$stage/vnet-list.ndjson"
    while IFS= read -r vnet; do
        id=$(jq -er '.id | strings | select(length > 0)' <<<"$vnet")
        context="VNet $id in subscription $subscription"
        printf '%s\n' "$vnet" | normalize \
            'include "inventory"; resource("virtualNetworks") + {location: (.location | text)}' >"$stage/vnet.json"
        path=$(resource_path "$id")
        list_all "$endpoint$path/virtualNetworkPeerings?api-version=2026-03-01" "$subscription" "$stage/peerings.json"
        normalize --slurpfile peerings "$stage/peerings.json" \
            'include "inventory"; . + {peerings: ($peerings[0] | peerings)}' "$stage/vnet.json" >>"$stage/vnets.ndjson"
    done <"$stage/vnet-list.ndjson"
done
context='staging output documents'
completed=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
jq --arg tenantId "$tenant_id" --arg inventoryId "$inventory_id" --arg started "$started" --arg completed "$completed" \
    '. + {schemaVersion: 1, tenantId: $tenantId, inventoryId: $inventoryId,
    collectionStartedAt: $started, collectionCompletedAt: $completed}' "$stage/coverage.json" >"$stage/envelope.json"
for kind in zones vnets; do
    if [[ "$kind" == zones ]]; then key=privateDnsZones; file=private-dns-zones.json
    else key=virtualNetworks; file=virtual-networks.json; fi
    jq -s 'sort_by(.id | ascii_downcase)' "$stage/$kind.ndjson" >"$stage/resources.json"
    jq --arg key "$key" --slurpfile resources "$stage/resources.json" \
        '. + {($key): $resources[0]}' "$stage/envelope.json" >"$stage/$file"
done
context='publishing output documents (a mismatched pair is rejected by the report)'
mv -f -- "$stage/private-dns-zones.json" "$output_directory/private-dns-zones.json"
mv -f -- "$stage/virtual-networks.json" "$output_directory/virtual-networks.json"
printf 'Inventory %s written to %s\n' "$inventory_id" "$output_directory" >&2

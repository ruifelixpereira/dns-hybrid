#!/usr/bin/env bash
set -euo pipefail
set +x

if [[ $# -ne 4 ]]; then
  echo "Usage: bash scripts/deploy.sh validate|what-if|create tenant-a|tenant-b TENANT_ID SUBSCRIPTION_ID" >&2
  exit 2
fi
action=$1
environment=$2
tenant=$3
subscription=$4
case "$action" in validate|what-if|create) ;; *) echo "Invalid action: $action" >&2; exit 2 ;; esac
case "$environment" in tenant-a|tenant-b) ;; *) echo "Invalid environment: $environment" >&2; exit 2 ;; esac
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

actual_tenant=$(az account show --subscription "$subscription" --query tenantId -o tsv)
if [[ "${actual_tenant,,}" != "${tenant,,}" ]]; then
  echo "Tenant mismatch for the specified subscription. Nothing deployed." >&2
  exit 1
fi

if [[ "$action" == create ]]; then
  read -r -p "Type '$environment/$subscription' to deploy billable resources: " confirmation
  if [[ "$confirmation" != "$environment/$subscription" ]]; then
    echo "Deployment cancelled." >&2
    exit 1
  fi
fi

trap 'unset DNS_ADMIN_PASSWORD' EXIT
read -r -s -p "Windows administrator password (12+ characters): " DNS_ADMIN_PASSWORD
printf '\n'
if [[ ${#DNS_ADMIN_PASSWORD} -lt 12 ]]; then
  echo "Password must contain at least 12 characters and meet Windows complexity requirements." >&2
  exit 2
fi
export DNS_ADMIN_PASSWORD

az deployment sub "$action" \
  --subscription "$subscription" \
  --name "dns-lab-$environment" \
  --location westeurope \
  --parameters "$root/parameters/$environment.bicepparam"

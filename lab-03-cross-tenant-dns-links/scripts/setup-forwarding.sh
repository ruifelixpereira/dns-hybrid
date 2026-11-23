#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

command -v python3 >/dev/null || { printf 'Error: python3 is required\n' >&2; exit 1; }
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_directory/forwarding.py" "$@"

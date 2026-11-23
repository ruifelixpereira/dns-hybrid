#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$script_directory/lab.sh" guests "$@"

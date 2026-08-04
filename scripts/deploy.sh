#!/usr/bin/env bash
#
# Deploy the hub to the BI host over SSH.
#
# Run from a machine with an ~/.ssh/config entry for the host (ours tunnels
# through AWS SSM, so no public SSH port is involved):
#
#     scripts/deploy.sh                # deploy origin/main to the `superset` host
#     scripts/deploy.sh --ref v1.2.0   # deploy a tag
#     scripts/deploy.sh --host other   # another ssh alias
#
# The host-side logic lives in scripts/host-update.sh and is piped over stdin,
# so this works even when the host's checkout predates that script.
set -euo pipefail

HOST="superset"
REF="origin/main"

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --ref)  REF="$2";  shift 2 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '\n\033[1m==> Deploying %s to %s\033[0m\n' "$REF" "$HOST"

# One SSH session for the whole update, so a dropped connection can't leave the
# host half-way between steps.
ssh -o ConnectTimeout=15 "$HOST" "bash -s -- '$REF'" < "$HERE/host-update.sh"

printf '\n\033[1m==> Done\033[0m\n'

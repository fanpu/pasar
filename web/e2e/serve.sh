#!/usr/bin/env bash
# Start a throwaway pasard for the browser smoke test: own data/config dirs, port 18750.
set -euo pipefail
root=$(mktemp -d)
export XDG_DATA_HOME="$root/data" XDG_CONFIG_HOME="$root/config" PASAR_ADDRESS=127.0.0.1:18750
cd "$(dirname "$0")/../.."
exec uv run pasard

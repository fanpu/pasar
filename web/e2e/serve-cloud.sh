#!/usr/bin/env bash
# Start a throwaway pasard with a fake cloud target for the cloud card's browser test: own data
# and config dirs, port 18751, seeded with a few jobs. See fake_cloud.py.
set -euo pipefail
root=$(mktemp -d)
export XDG_DATA_HOME="$root/data" XDG_CONFIG_HOME="$root/config"
cd "$(dirname "$0")/../.."

pid=""
cleanup() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -rf "$root"
}
trap cleanup EXIT INT TERM

uv run python web/e2e/fake_cloud.py --data "$root/pasar" --port "${PASAR_CLOUD_PORT:-18751}" --seed &
pid=$!
wait "$pid"

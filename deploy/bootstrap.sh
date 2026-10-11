#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${OCTOP_ROOT:-/data/srv/octop}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run this script as root" >&2
  exit 1
fi

for command in curl docker flock install; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done
docker compose version >/dev/null

install -d -m 0755 "$ROOT_DIR/deploy" "$ROOT_DIR/deploy/history"
# Shared Octop home, mounted at /data/.octop inside the container.
install -d -m 0700 "$ROOT_DIR/.octop"
install -m 0755 \
  "$SCRIPT_DIR/bootstrap.sh" \
  "$SCRIPT_DIR/deploy.sh" \
  "$SCRIPT_DIR/rollback.sh" \
  "$ROOT_DIR/deploy/"
install -m 0644 \
  "$SCRIPT_DIR/compose.yaml" \
  "$SCRIPT_DIR/config.json.example" \
  "$SCRIPT_DIR/release.env.example" \
  "$SCRIPT_DIR/preflight.py" \
  "$ROOT_DIR/deploy/"

if [[ ! -f "$ROOT_DIR/.octop/config.json" ]]; then
  install -m 0600 "$SCRIPT_DIR/config.json.example" "$ROOT_DIR/.octop/config.json"
  echo "created $ROOT_DIR/.octop/config.json; fill in the database section before deployment"
fi

echo "Octop deployment directory initialized at $ROOT_DIR"
echo "Next: fill .octop/config.json (RDS), log root in to ACR, then push an app-YYYYMMDD.N tag."

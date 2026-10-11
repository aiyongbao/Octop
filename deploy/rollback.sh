#!/usr/bin/env bash
set -euo pipefail

# Only for releases without schema changes: Octop migrations are forward-only.
ROOT_DIR="${OCTOP_ROOT:-/data/srv/octop}"
DEPLOY_DIR="$ROOT_DIR/deploy"
COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
RELEASE_ENV="$DEPLOY_DIR/release.env"
HISTORY_FILE="${1:-}"
LOCK_FILE="$DEPLOY_DIR/.deploy.lock"
DOCKER_CONFIG_DIR="${DOCKER_CONFIG:-/root/.docker}"

docker_command() {
  docker --config "$DOCKER_CONFIG_DIR" "$@"
}

for command in curl docker find flock; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done
docker_command compose version >/dev/null

if [[ -z "$HISTORY_FILE" ]]; then
  HISTORY_FILE="$(find "$DEPLOY_DIR/history" -maxdepth 1 -type f -name '*.env' -print | sort | tail -1)"
elif [[ "$HISTORY_FILE" != /* ]]; then
  HISTORY_FILE="$DEPLOY_DIR/history/$HISTORY_FILE"
fi

[[ -f "$HISTORY_FILE" ]] || { echo "rollback history not found" >&2; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo "missing $COMPOSE_FILE" >&2; exit 1; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another Octop deployment is running" >&2
  exit 1
fi

cp "$HISTORY_FILE" "$RELEASE_ENV"
chmod 0600 "$RELEASE_ENV"
docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" pull octop
docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" up -d --no-deps octop

for _ in $(seq 1 100); do
  if curl -fsS --connect-timeout 2 --max-time 5 \
    http://127.0.0.1:8088/api/health >/dev/null; then
    echo "Octop rollback completed from $HISTORY_FILE"
    exit 0
  fi
  sleep 3
done

docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" logs --tail 200 octop >&2 || true
echo "rollback image did not become healthy" >&2
exit 1

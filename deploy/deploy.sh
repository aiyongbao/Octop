#!/usr/bin/env bash
set -euo pipefail

OCTOP_IMAGE=""
OCTOP_RELEASE=""
ROOT_DIR="${OCTOP_ROOT:-/data/srv/octop}"
HEALTH_TIMEOUT_SECONDS="${DEPLOY_HEALTH_TIMEOUT_SECONDS:-300}"
DOCKER_CONFIG_DIR="${DOCKER_CONFIG:-/root/.docker}"
HEALTH_URL="http://127.0.0.1:8088/api/health"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) OCTOP_IMAGE="${2:-}"; shift 2 ;;
    --release) OCTOP_RELEASE="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! "$OCTOP_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "--image must be an immutable ACR image digest" >&2
  exit 2
fi
if [[ ! "$OCTOP_RELEASE" =~ ^app-[0-9]{8}\.[0-9]+$ ]]; then
  echo "--release must use app-YYYYMMDD.N" >&2
  exit 2
fi
if [[ ! "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "DEPLOY_HEALTH_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

docker_command() {
  docker --config "$DOCKER_CONFIG_DIR" "$@"
}

# Stage 1: pull the image and re-exec the deploy.sh shipped inside it, so
# script fixes roll out together with the release they belong to.
if [[ "${OCTOP_DEPLOY_DELEGATED:-0}" != "1" ]]; then
  for command in docker mktemp; do
    command -v "$command" >/dev/null || {
      echo "missing required command: $command" >&2
      exit 1
    }
  done

  ASSET_STAGE="$(mktemp -d /tmp/symw-octop-deploy.XXXXXX)"
  ASSET_CONTAINER=""
  cleanup_assets() {
    [[ -z "$ASSET_CONTAINER" ]] || docker_command rm -f "$ASSET_CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$ASSET_STAGE"
  }
  trap cleanup_assets EXIT

  if [[ ! -r "$DOCKER_CONFIG_DIR/config.json" ]]; then
    echo "missing Docker registry credentials: $DOCKER_CONFIG_DIR/config.json" >&2
    echo "log root in to the ACR VPC registry before deploying" >&2
    exit 1
  fi
  if ! docker_command pull "$OCTOP_IMAGE"; then
    echo "failed to pull the private ACR image: $OCTOP_IMAGE" >&2
    exit 1
  fi

  ASSET_CONTAINER="$(docker_command create "$OCTOP_IMAGE")"
  docker_command cp "$ASSET_CONTAINER:/opt/deployment/." "$ASSET_STAGE/"
  docker_command rm "$ASSET_CONTAINER" >/dev/null
  ASSET_CONTAINER=""
  chmod 0755 "$ASSET_STAGE"/*.sh

  trap - EXIT
  OCTOP_DEPLOY_DELEGATED=1 \
  OCTOP_DEPLOY_ASSET_DIR="$ASSET_STAGE" \
  DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
    exec "$ASSET_STAGE/deploy.sh" --image "$OCTOP_IMAGE" --release "$OCTOP_RELEASE"
fi

DEPLOY_DIR="$ROOT_DIR/deploy"
DATA_DIR="$ROOT_DIR/.octop"
ASSET_DIR="${OCTOP_DEPLOY_ASSET_DIR:-$DEPLOY_DIR}"
COMPOSE_FILE="$ASSET_DIR/compose.yaml"
CURRENT_COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
RELEASE_ENV="$DEPLOY_DIR/release.env"
HISTORY_DIR="$DEPLOY_DIR/history"
LOCK_FILE="$DEPLOY_DIR/.deploy.lock"
NEXT_ENV=""
PREVIOUS_ENV=""

cleanup() {
  [[ -z "$NEXT_ENV" ]] || rm -f "$NEXT_ENV"
  if [[ "$ASSET_DIR" != "$DEPLOY_DIR" ]]; then
    rm -rf "$ASSET_DIR"
  fi
}
trap cleanup EXIT

for command in curl docker flock; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done
docker_command compose version >/dev/null

mkdir -p "$HISTORY_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another Octop deployment is running" >&2
  exit 1
fi

[[ -f "$COMPOSE_FILE" ]] || { echo "missing $COMPOSE_FILE" >&2; exit 1; }
[[ -d "$DATA_DIR" ]] || {
  echo "missing $DATA_DIR; run bootstrap.sh first" >&2
  exit 1
}

# Database lives in .octop/config.json; refuse to start against SQLite or
# placeholder credentials, and make sure RDS is reachable from a container.
docker_command run --rm --network bridge \
  --entrypoint python \
  -e HOME=/data \
  -v "$DATA_DIR:/data/.octop:ro" \
  "$OCTOP_IMAGE" /opt/deployment/preflight.py

umask 077
NEXT_ENV="$DEPLOY_DIR/.release.$$.env"
printf 'OCTOP_RELEASE=%s\nOCTOP_IMAGE=%s\n' "$OCTOP_RELEASE" "$OCTOP_IMAGE" >"$NEXT_ENV"
docker_command compose --env-file "$NEXT_ENV" -f "$COMPOSE_FILE" config --quiet

if [[ -f "$RELEASE_ENV" ]]; then
  PREVIOUS_RELEASE="$(awk -F= '$1 == "OCTOP_RELEASE" { print $2; exit }' "$RELEASE_ENV")"
  PREVIOUS_ENV="$HISTORY_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${PREVIOUS_RELEASE:-unknown}.env"
  cp "$RELEASE_ENV" "$PREVIOUS_ENV"
  chmod 0600 "$PREVIOUS_ENV"
fi

wait_for_health() {
  local timeout_seconds="$1"
  local deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if curl -fsS --connect-timeout 2 --max-time 5 "$HEALTH_URL" >/dev/null; then
      return 0
    fi
    sleep 3
  done
  return 1
}

restore_previous_release_env() {
  if [[ -n "$PREVIOUS_ENV" && -f "$PREVIOUS_ENV" ]]; then
    cp "$PREVIOUS_ENV" "$RELEASE_ENV"
    chmod 0600 "$RELEASE_ENV"
  else
    rm -f "$RELEASE_ENV"
  fi
}

rollback_previous() {
  if [[ -z "$PREVIOUS_ENV" || ! -f "$PREVIOUS_ENV" ]]; then
    restore_previous_release_env
    echo "deployment failed and no previous release is available" >&2
    return 1
  fi

  echo "deployment failed; rolling back with $PREVIOUS_ENV" >&2
  restore_previous_release_env
  local rollback_compose="$CURRENT_COMPOSE_FILE"
  if [[ ! -f "$rollback_compose" ]]; then
    rollback_compose="$COMPOSE_FILE"
  fi
  docker_command compose --env-file "$RELEASE_ENV" -f "$rollback_compose" pull octop
  docker_command compose --env-file "$RELEASE_ENV" -f "$rollback_compose" up -d --no-deps octop
  wait_for_health 300
}

mv "$NEXT_ENV" "$RELEASE_ENV"
NEXT_ENV=""

if ! docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" pull octop; then
  rollback_previous || true
  exit 1
fi
if ! docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" up -d --no-deps octop; then
  rollback_previous
  exit 1
fi
if ! wait_for_health "$HEALTH_TIMEOUT_SECONDS"; then
  docker_command compose --env-file "$RELEASE_ENV" -f "$COMPOSE_FILE" logs --tail 200 octop >&2 || true
  rollback_previous
  exit 1
fi

if [[ "$ASSET_DIR" != "$DEPLOY_DIR" ]]; then
  install -m 0644 \
    "$ASSET_DIR/compose.yaml" \
    "$ASSET_DIR/config.json.example" \
    "$ASSET_DIR/release.env.example" \
    "$ASSET_DIR/preflight.py" \
    "$DEPLOY_DIR/"
  install -m 0755 \
    "$ASSET_DIR/bootstrap.sh" \
    "$ASSET_DIR/deploy.sh" \
    "$ASSET_DIR/rollback.sh" \
    "$DEPLOY_DIR/"
fi

echo "Octop deployment completed: release=$OCTOP_RELEASE image=$OCTOP_IMAGE"

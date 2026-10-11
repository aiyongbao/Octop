#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-}"
RELEASE="${2:-}"

if [[ ! "$IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "image must use an immutable digest" >&2
  exit 2
fi
if [[ ! "$RELEASE" =~ ^app-[0-9]{8}\.[0-9]+$ ]]; then
  echo "release must use app-YYYYMMDD.N" >&2
  exit 2
fi

REGION_ID="${ECS_REGION_ID:?ECS_REGION_ID is required}"
INSTANCE_ID="${ECS_INSTANCE_ID:?ECS_INSTANCE_ID is required}"

REMOTE_COMMAND="/data/srv/octop/deploy/deploy.sh --image '$IMAGE' --release '$RELEASE'"
COMMAND_CONTENT="$(printf '%s' "$REMOTE_COMMAND" | base64 | tr -d '\n')"

RUN_RESULT="$(aliyun ecs RunCommand \
  --RegionId "$REGION_ID" \
  --InstanceId.1 "$INSTANCE_ID" \
  --Type RunShellScript \
  --CommandContent "$COMMAND_CONTENT" \
  --ContentEncoding Base64 \
  --Timeout 1800 \
  --Username root \
  --WorkingDir /data/srv/octop \
  --Name "symw-octop-$RELEASE")"
INVOKE_ID="$(jq -er '.InvokeId' <<<"$RUN_RESULT")"
echo "Cloud Assistant invocation started: $INVOKE_ID"

for _ in $(seq 1 180); do
  INVOCATION="$(aliyun ecs DescribeInvocations \
    --RegionId "$REGION_ID" \
    --InvokeId "$INVOKE_ID")"
  STATUS="$(jq -r '.Invocations.Invocation[0].InvocationStatus // "Pending"' <<<"$INVOCATION")"
  case "$STATUS" in
    Success)
      OUTPUT="$(jq -r '.Invocations.Invocation[0].InvokeInstances.InvokeInstance[0].Output // empty' <<<"$INVOCATION")"
      if [[ -n "$OUTPUT" ]]; then
        printf '%s' "$OUTPUT" | base64 --decode || true
        printf '\n'
      fi
      echo "Cloud Assistant invocation succeeded: $INVOKE_ID"
      exit 0
      ;;
    Failed|Stopped|PartialFailed)
      OUTPUT="$(jq -r '.Invocations.Invocation[0].InvokeInstances.InvokeInstance[0].Output // empty' <<<"$INVOCATION")"
      if [[ -n "$OUTPUT" ]]; then
        printf '%s' "$OUTPUT" | base64 --decode >&2 || true
        printf '\n' >&2
      fi
      echo "Cloud Assistant invocation failed: status=$STATUS id=$INVOKE_ID" >&2
      exit 1
      ;;
    Pending|Running|Stopping)
      sleep 10
      ;;
    *)
      echo "unexpected Cloud Assistant status: $STATUS" >&2
      sleep 10
      ;;
  esac
done

echo "Cloud Assistant invocation timed out: $INVOKE_ID" >&2
exit 1

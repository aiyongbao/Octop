#!/usr/bin/env bash
# Local Trivy gate for the symw image. Same Trivy version and thresholds as
# the "Scan candidate digest" step in .github/workflows/symw-release.yml, so
# blocking findings surface before a tag is pushed.
#
#   deploy/scan.sh                 build linux/amd64 image, then scan it
#   SKIP_BUILD=1 deploy/scan.sh    scan the existing local image
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE="${IMAGE:-symw-octop:local}"
TRIVY_IMAGE="aquasec/trivy:0.70.0"
TRIVY_CACHE="${TRIVY_CACHE:-$HOME/.cache/trivy}"

if [[ -z "${SKIP_BUILD:-}" ]]; then
  docker buildx bake -f deploy/docker-bake.hcl --load --set "symw.tags=$IMAGE" symw
fi

mkdir -p "$TRIVY_CACHE"
trivy_args=(
  --rm
  -v /var/run/docker.sock:/var/run/docker.sock
  -v "$TRIVY_CACHE:/root/.cache/trivy"
)
ignore_args=()
if [[ -f .trivyignore ]]; then
  trivy_args+=(-v "$PWD/.trivyignore:/work/.trivyignore:ro")
  ignore_args=(--ignorefile /work/.trivyignore)
fi

echo "== Report: HIGH + CRITICAL (informational) =="
docker run "${trivy_args[@]}" "$TRIVY_IMAGE" image \
  --scanners vuln --severity HIGH,CRITICAL --format table \
  ${ignore_args[@]+"${ignore_args[@]}"} "$IMAGE"

echo "== Gate: CRITICAL with a fix available (same as CI) =="
docker run "${trivy_args[@]}" "$TRIVY_IMAGE" image \
  --scanners vuln --severity CRITICAL --ignore-unfixed --exit-code 1 --format table \
  ${ignore_args[@]+"${ignore_args[@]}"} "$IMAGE"
echo "scan passed: $IMAGE"

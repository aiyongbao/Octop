# 生意喵 (symw) image build. From the repo root:
#   docker buildx bake -f deploy/docker-bake.hcl --load symw
# CI overrides tags / cache / push via bake-action ``set``.

variable "IMAGE" {
  default = "symw-octop:local"
}

# Upstream runtime stage, unchanged except the base image is pinned by digest
# (bump deliberately, then rerun deploy/scan.sh).
target "octop-runtime" {
  context    = "."
  dockerfile = "docker/Dockerfile"
  target     = "runtime"
  platforms  = ["linux/amd64"]
  contexts = {
    "python:3.12-slim" = "docker-image://python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
  }
}

target "symw" {
  context    = "."
  dockerfile = "deploy/Dockerfile"
  contexts = {
    octop-runtime = "target:octop-runtime"
  }
  platforms = ["linux/amd64"]
  tags      = [IMAGE]
}

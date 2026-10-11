# syntax=docker/dockerfile:1.7
# 生意喵 (symw) production image: upstream Octop runtime + deployment assets.
# Built via deploy/docker-bake.hcl, which supplies ``octop-runtime`` from
# docker/Dockerfile so upstream files stay untouched.

FROM octop-runtime

# Security overrides (see security-overrides.txt). Upstream sets HOME=/data but
# mounts the uv cache at /root/.cache/uv, so /data/.cache/uv leaks into the
# image; drop it so stale wheels are neither shipped to agents nor scanned.
COPY deploy/security-overrides.txt /tmp/security-overrides.txt
RUN uv pip install --python /app/.venv/bin/python --no-cache -r /tmp/security-overrides.txt \
    && uv pip check --python /app/.venv/bin/python \
    && rm -rf /data/.cache/uv /tmp/security-overrides.txt

COPY deploy/ /opt/deployment/
RUN chmod 0755 /opt/deployment/*.sh

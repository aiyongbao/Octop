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

# Data-analysis libraries agents use (see agent-packages.txt). Installed into
# Octop's venv so the agent env in /data/pyenv (deploy/entrypoint.sh) sees them.
COPY deploy/agent-packages.txt /tmp/agent-packages.txt
RUN uv pip install --python /app/.venv/bin/python --no-cache -r /tmp/agent-packages.txt \
    && uv pip check --python /app/.venv/bin/python \
    && rm -rf /data/.cache/uv /tmp/agent-packages.txt

# Install the company fork after all registry-based layers. The fork keeps the
# upstream version, so force replacement and verify Git provenance plus files.
# No dependency resolution here: retain uv.lock/security overrides, then check
# compatibility. A missing commit or a mismatch must fail the image build.
COPY deploy/gateway-requirements.txt deploy/gateway-source.json deploy/verify_gateway.py /tmp/gateway-build/
RUN uv pip install --python /app/.venv/bin/python --no-cache --no-deps \
        --reinstall-package octop-gateway -r /tmp/gateway-build/gateway-requirements.txt \
    && uv pip check --python /app/.venv/bin/python \
    && /app/.venv/bin/python /tmp/gateway-build/verify_gateway.py \
    && rm -rf /data/.cache/uv /tmp/gateway-build

COPY deploy/ /opt/deployment/
RUN chmod 0755 /opt/deployment/*.sh

# Agents' persistent Python env (created by entrypoint.sh) wins on PATH, so
# their `python3` / `pip` stay consistent; it is absent in one-off containers
# (e.g. deploy.sh preflight), which fall through to /app/.venv as before.
ENV PATH="/data/pyenv/bin:$PATH" \
    MATPLOTLIBRC=/opt/deployment/matplotlibrc

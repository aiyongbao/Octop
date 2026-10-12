#!/usr/bin/env bash
# symw container entrypoint: prepare the agents' persistent Python env, then
# exec the given command (compose passes `octop run ...`).
#
# /data/pyenv is a bind mount, so `pip install` from agents survives image
# upgrades. It is first on PATH (deploy/Dockerfile), and sees every package in
# /app/.venv through a .pth file, so agents keep what they had while their own
# installs land in /data/pyenv and never touch Octop's runtime venv.
#
# Setup failures are logged but never block Octop: without a working
# /data/pyenv, PATH falls through to /app/.venv as before.
set -uo pipefail

bash -euo pipefail <<'SETUP' || echo "[entrypoint] WARN: agent Python env setup failed; agents fall back to /app/.venv" >&2
PYENV=/data/pyenv
OCTOP_PY=/app/.venv/bin/python
py_version='import sys; print("%d.%d" % sys.version_info[:2])'
purelib='import sysconfig; print(sysconfig.get_paths()["purelib"])'

want="$("$OCTOP_PY" -c "$py_version")"
have="$("$PYENV/bin/python" -c "$py_version" 2>/dev/null || true)"
if [[ "$have" != "$want" ]]; then
  # --clear empties the directory in place; /data/pyenv itself is a mount point.
  echo "[entrypoint] creating agent Python env at $PYENV (python $want, was: ${have:-none})"
  "$OCTOP_PY" -m venv --clear "$PYENV"
fi

# Rewritten on every boot so it always follows the current image's venv.
printf 'import site; site.addsitedir(%s)\n' "'$("$OCTOP_PY" -c "$purelib")'" \
  >"$("$PYENV/bin/python" -c "$purelib")/octop-runtime.pth"
SETUP

exec "$@"

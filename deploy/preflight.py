"""Pre-deploy check, run inside the release image by deploy.sh.

Fails unless /data/.octop/config.json selects PostgreSQL with real
credentials and the database answers. Never prints the password.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG = Path("/data/.octop/config.json")


def fail(msg: str) -> None:
    print(f"preflight: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    leaked = [k for k in os.environ if k.startswith("OCTOP_DATABASE_")]
    if leaked:
        fail(f"unexpected env overrides {leaked}; keep database settings in {CONFIG}")
    if not CONFIG.is_file():
        fail(f"missing {CONFIG}; run bootstrap.sh and fill in the database section")

    import psycopg

    from octop.config import load_config

    db = load_config(CONFIG).database
    if not db.is_postgresql:
        fail(f"database.driver must be postgresql, got {db.driver!r}")
    if not db.url and (not db.password or "replace-me" in f"{db.host}{db.password}"):
        fail("database host/password still hold placeholder values")
    try:
        with psycopg.connect(db.postgresql_conninfo(), connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        fail(f"cannot reach PostgreSQL at {db.host}:{db.port}/{db.database}: {exc}")
    print(f"preflight: PostgreSQL {db.host}:{db.port}/{db.database} reachable")


if __name__ == "__main__":
    main()

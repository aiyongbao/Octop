#!/usr/bin/env python3
"""Copy a local 生意喵 workspace into a deployed Octop agent over HTTPS.

Usage (from any machine that can reach the site):

    OCTOP_PASSWORD=... python3 deploy/tools/push_workspace.py \\
        --base-url https://symw.aiyongtech.com --username admin \\
        --agent <server agent_id> --source ~/.octop/workspaces/KWCEBW --dry-run

Copies the prompt files, ``.octop/skills/`` (minus ``.trash``),
``.octop/agents/`` and ``.octop/.env`` (agent env such as SHENGYIMIAO_TOKEN),
then reloads the agent. The logged-in user must own the agent.
Standard library only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PROMPT_FILES = ("SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md", "PROACTIVE.md")
SYSTEM_TREES = (".octop/skills", ".octop/agents")
SYSTEM_FILES = (".octop/.env",)
SKIP_NAMES = {".trash", "__pycache__", ".DS_Store", "node_modules"}


def iter_files(source: Path) -> Iterator[str]:
    for name in (*PROMPT_FILES, *SYSTEM_FILES):
        if (source / name).is_file():
            yield name
    for tree in SYSTEM_TREES:
        root = source / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(source)
            if path.is_file() and not SKIP_NAMES.intersection(rel.parts):
                yield rel.as_posix()


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + "/api"
        self.token = ""

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict:
        headers = {"Content-Type": content_type}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(self.base + path, data=body, method=method, headers=headers)
        # Retry connection-level failures (TLS resets, timeouts); every call
        # here is safe to repeat. HTTP errors are real answers and propagate.
        for attempt in range(1, 7):
            try:
                with urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else {}
            except HTTPError:
                raise
            except (URLError, OSError) as exc:
                if attempt == 6:
                    raise
                print(
                    f"     retry {attempt}/5 {method} {path.split('?')[0]}: {exc}", file=sys.stderr
                )
                time.sleep(min(2**attempt, 20))
        raise AssertionError("unreachable")

    def login(self, username: str, password: str) -> None:
        body = json.dumps({"username": username, "password": password}).encode()
        self.token = self.call("POST", "/auth/login", body)["access_token"]

    def put_text(self, agent: str, rel: str, text: str) -> None:
        q = f"?path={quote(rel, safe='')}&from_workspace=true"
        body = json.dumps({"content": text}).encode()
        self.call("PUT", f"/agents/{agent}/workspace/file{q}", body)

    def upload(self, agent: str, rel: str, data: bytes) -> None:
        boundary = uuid.uuid4().hex
        name = rel.rsplit("/", 1)[-1]
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        q = f"?path={quote(rel, safe='')}&from_workspace=true"
        self.call(
            "POST",
            f"/agents/{agent}/workspace/upload{q}",
            body,
            f"multipart/form-data; boundary={boundary}",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--agent", required=True, help="agent_id on the server")
    ap.add_argument("--source", required=True, type=Path, help="local workspace dir")
    ap.add_argument("--dry-run", action="store_true", help="list files only")
    args = ap.parse_args()

    source = args.source.expanduser()
    if not source.is_dir():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    files = list(iter_files(source))
    print(f"{len(files)} files from {source}")
    if args.dry_run:
        print("\n".join(files))
        return 0

    client = Client(args.base_url)
    password = os.environ.get("OCTOP_PASSWORD") or getpass.getpass("Password: ")
    client.login(args.username, password)

    failed = 0
    for rel in files:
        data = (source / rel).read_bytes()
        try:
            try:
                client.put_text(args.agent, rel, data.decode("utf-8"))
            except UnicodeDecodeError:
                client.upload(args.agent, rel, data)
            print(f"ok   {rel}")
        except HTTPError as exc:
            failed += 1
            print(f"FAIL {rel}: HTTP {exc.code} {exc.read()[:200]!r}", file=sys.stderr)

    client.call("POST", f"/agents/{args.agent}/reload")
    print(f"done: {len(files) - failed} copied, {failed} failed; agent reloaded")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

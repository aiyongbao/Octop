"""Validate the company gateway pin and the exact package installed in an image."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import re
from pathlib import Path
from typing import Any


def read_pin(directory: Path) -> dict[str, Any]:
    """Keep the install requirement and file manifest tied to one immutable commit."""
    pin = json.loads((directory / "gateway-source.json").read_text(encoding="utf-8"))
    if pin["repository"] != "https://github.com/aiyongbao/octop-gateway.git":
        raise ValueError("gateway repository must be the company fork")
    if not re.fullmatch(r"[0-9a-f]{40}", pin["commit"]):
        raise ValueError("gateway commit must be a full immutable SHA")
    requirements = [
        line.strip()
        for line in (directory / "gateway-requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = f"octop-gateway @ git+{pin['repository']}@{pin['commit']}"
    if requirements != [expected]:
        raise ValueError("gateway requirement and manifest disagree")
    files = pin["files"]
    required = {"octop_gateway/channels/feishu_cards.py", "octop_gateway/assets/feishu-loading.gif"}
    if not required <= files.keys():
        raise ValueError("gateway manifest must include the card renderer and animation")
    for name, digest in files.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "octop_gateway":
            raise ValueError("gateway manifest contains an invalid package path")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("gateway file digest must be SHA-256")
    return pin


def verify_gateway(directory: Path) -> dict[str, Any]:
    """Reject a registry package, wrong commit, shadowed import or incomplete wheel."""
    pin = read_pin(directory)
    dist = importlib.metadata.distribution("octop-gateway")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    vcs = direct.get("vcs_info", {})
    if direct.get("url") != pin["repository"] or vcs.get("vcs") != "git":
        raise ValueError("installed gateway did not come from the company Git repository")
    if vcs.get("commit_id") != pin["commit"] or dist.version != pin["version"]:
        raise ValueError("installed gateway commit/version does not match the release pin")
    spec = importlib.util.find_spec("octop_gateway")
    expected_origin = Path(dist.locate_file("octop_gateway/__init__.py")).resolve()
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected_origin:
        raise ValueError("gateway import is shadowed by another installation")
    for name, digest in pin["files"].items():
        path = Path(dist.locate_file(name))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"gateway package content does not match the release pin: {name}")
    return {
        "ok": True,
        "commit": pin["commit"],
        "version": dist.version,
        "verified_files": len(pin["files"]),
    }


def main() -> None:
    """Support cheap CI pin checks and a full build-time installation check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-pin", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    if args.check_pin:
        pin = read_pin(directory)
        result = {"ok": True, "commit": pin["commit"], "verified_files": len(pin["files"])}
    else:
        result = verify_gateway(directory)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

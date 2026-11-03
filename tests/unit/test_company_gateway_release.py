"""Company image releases must carry the pinned fork, including non-Python assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy import verify_gateway as verifier


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    files = {
        "octop_gateway/__init__.py": b"",
        "octop_gateway/channels/feishu_cards.py": b"# renderer\n",
        "octop_gateway/assets/feishu-loading.gif": b"GIF89a",
    }
    pin = {
        "repository": "https://github.com/aiyongbao/octop-gateway.git",
        "commit": "a" * 40,
        "version": "1.0.3",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (tmp_path / "gateway-source.json").write_text(json.dumps(pin), encoding="utf-8")
    (tmp_path / "gateway-requirements.txt").write_text(
        f"octop-gateway @ git+{pin['repository']}@{pin['commit']}\n", encoding="utf-8"
    )
    provenance = {"url": pin["repository"], "vcs_info": {"vcs": "git", "commit_id": pin["commit"]}}
    dist = SimpleNamespace(
        version=pin["version"],
        read_text=lambda _name: json.dumps(provenance),
        locate_file=lambda name: tmp_path / name,
    )
    monkeypatch.setattr(verifier.importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(
        verifier.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(tmp_path / "octop_gateway/__init__.py")),
    )
    return tmp_path, provenance, dist


def test_pinned_company_installation_passes(installed):
    directory, _, _ = installed
    assert verifier.verify_gateway(directory) == {
        "ok": True,
        "commit": "a" * 40,
        "version": "1.0.3",
        "verified_files": 3,
    }


def test_same_version_from_pypi_is_rejected(installed):
    directory, provenance, _ = installed
    provenance.clear()
    with pytest.raises(ValueError, match="company Git repository"):
        verifier.verify_gateway(directory)


def test_different_company_commit_is_rejected(installed):
    directory, provenance, _ = installed
    provenance["vcs_info"]["commit_id"] = "b" * 40
    with pytest.raises(ValueError, match="commit/version"):
        verifier.verify_gateway(directory)


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_modified_animation_fails_build(installed, missing):
    directory, _, _ = installed
    asset = directory / "octop_gateway/assets/feishu-loading.gif"
    if missing:
        asset.unlink()
    else:
        asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="feishu-loading.gif"):
        verifier.verify_gateway(directory)


def test_shadowed_import_is_rejected(installed, monkeypatch):
    directory, _, _ = installed
    monkeypatch.setattr(
        verifier.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="other/__init__.py"),
    )
    with pytest.raises(ValueError, match="shadowed"):
        verifier.verify_gateway(directory)


def test_floating_requirement_is_rejected(installed):
    directory, _, _ = installed
    (directory / "gateway-requirements.txt").write_text("octop-gateway>=1.0.3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="disagree"):
        verifier.read_pin(directory)


def test_company_build_installs_and_verifies_pin_after_other_dependencies():
    repo = Path(__file__).resolve().parents[2]
    assert len(verifier.read_pin(repo / "deploy")["files"]) >= 40
    dockerfile = (repo / "deploy/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.index("/tmp/agent-packages.txt") < dockerfile.index(
        "--reinstall-package octop-gateway"
    )
    assert "--no-deps" in dockerfile
    assert dockerfile.index("--reinstall-package octop-gateway") < dockerfile.index(
        "python /tmp/gateway-build/verify_gateway.py"
    )
    workflow = (repo / ".github/workflows/symw-release.yml").read_text(encoding="utf-8")
    assert "python3 deploy/verify_gateway.py --check-pin" in workflow

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import stagemesh.runtime_info as ri


def source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "src" / "stagemesh"
    package.mkdir(parents=True)
    module = package / "runtime_info.py"
    module.write_text("# source fixture\n", encoding="utf-8")
    monkeypatch.setattr(ri, "__file__", str(module))
    monkeypatch.setattr(ri, "_dist", lambda: None)
    return package


def init_source(package: Path) -> str:
    root = package.parent.parent
    for command in (
        ["init", "--quiet"],
        ["add", "src/stagemesh/runtime_info.py"],
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=", "commit", "--quiet", "-m", "source fixture"],
    ):
        subprocess.run(["git", *command], cwd=root, capture_output=True, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def test_loaded_checkout_records_its_own_commit_and_dirty_state(tmp_path, monkeypatch):
    package = source_tree(tmp_path, monkeypatch)
    commit = init_source(package)
    # A wheel build module must never override the actual loaded checkout's Git identity.
    monkeypatch.setattr(ri, "_build_info", lambda: {"source_commit": "f" * 40, "source_dirty": False})
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    evidence = ri.runtime_evidence()
    assert evidence["provenance"] == {"kind": "source_checkout", "source_commit": commit, "source_dirty": False, "status": "VERIFIED"}
    assert evidence["package_path"] == str(package.resolve())
    assert evidence["process_identity"]["pid"]
    assert evidence["interpreter"]
    (package / "runtime_info.py").write_text("# modified\n", encoding="utf-8")
    assert ri.runtime_evidence()["provenance"]["source_dirty"] is True


def test_missing_git_repository_keeps_provenance_unknown(tmp_path, monkeypatch):
    source_tree(tmp_path, monkeypatch)
    provenance = ri.runtime_evidence()["provenance"]
    assert provenance["source_commit"] is None and provenance["source_dirty"] is None
    assert provenance["status"] == "UNKNOWN"


def test_unrelated_enclosing_repository_does_not_identify_the_loaded_package(tmp_path, monkeypatch):
    package = source_tree(tmp_path, monkeypatch)
    init_source(package)
    other = tmp_path / "unrelated" / "src" / "stagemesh"
    other.mkdir(parents=True)
    monkeypatch.setattr(ri, "__file__", str(other / "runtime_info.py"))
    provenance = ri.runtime_evidence()["provenance"]
    assert provenance["status"] == "UNKNOWN"
    assert provenance["source_commit"] is None
    assert provenance["reason"] == "LOADED_PACKAGE_NOT_VERIFIED"


def test_untracked_package_does_not_inherit_repository_identity(tmp_path, monkeypatch):
    package = source_tree(tmp_path, monkeypatch)
    init_source(package)
    subprocess.run(["git", "rm", "--cached", "src/stagemesh/runtime_info.py"], cwd=tmp_path, capture_output=True, check=True)
    provenance = ri.runtime_evidence()["provenance"]
    assert provenance["status"] == "UNKNOWN"
    assert provenance["source_commit"] is None


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("git", 2), subprocess.CalledProcessError(1, "git")])
def test_git_failures_are_bounded_unknown_evidence(tmp_path, monkeypatch, failure):
    source_tree(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 2
        assert kwargs["check"] is True
        raise failure

    monkeypatch.setattr(ri.subprocess, "run", fail)
    assert ri.runtime_evidence()["provenance"]["status"] == "UNKNOWN"


def test_wheel_uses_only_embedded_build_identity_and_does_not_probe_git(tmp_path, monkeypatch):
    package = tmp_path / "site-packages" / "stagemesh"
    monkeypatch.setattr(ri, "__file__", str(package / "runtime_info.py"))
    monkeypatch.setattr(ri, "_dist", lambda: None)
    monkeypatch.setattr(ri, "_build_info", lambda: {"source_commit": "a" * 40, "source_dirty": True, "built_from": "private-checkout", "install_source": "https://user:secret@example.invalid"})
    monkeypatch.setattr(ri, "_source_evidence", lambda _: pytest.fail("wheel must not inherit an enclosing repository"))
    evidence = ri.runtime_evidence()
    assert evidence["provenance"] == {"kind": "wheel_build", "source_commit": "a" * 40, "source_dirty": True, "status": "VERIFIED"}
    assert "secret" not in json.dumps(evidence)
    assert "private-checkout" not in json.dumps(evidence)


def test_wheel_without_build_metadata_preserves_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "__file__", str(tmp_path / "site-packages" / "stagemesh" / "runtime_info.py"))
    monkeypatch.setattr(ri, "_dist", lambda: None)
    monkeypatch.setattr(ri, "_build_info", dict)
    provenance = ri.runtime_evidence()["provenance"]
    assert provenance["status"] == "UNKNOWN"
    assert provenance["source_commit"] is None and provenance["source_dirty"] is None

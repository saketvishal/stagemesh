from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import build_backend

ROOT = Path(__file__).resolve().parents[1]


def _verifier():
    spec = importlib.util.spec_from_file_location("verify_pypi_distribution", ROOT / "scripts/verify_pypi_distribution.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_workflow_is_explicit_oidc_and_exact_artifact_only() -> None:
    workflow = (ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
    assert "release:" in workflow and "types: [published]" in workflow
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "skip-existing: false" in workflow
    assert "attestations: true" in workflow
    assert "actions/upload-artifact@v4" in workflow and "actions/download-artifact@v4" in workflow
    assert "verify_pypi_distribution.py" in workflow
    assert "password:" not in workflow and "PYPI_API_TOKEN" not in workflow


def _clean_build_info(_root: Path) -> bytes:
    return ("SOURCE_COMMIT = " + repr("f" * 40) + "\nSOURCE_DIRTY = False\nBUILT_FROM = 'test'\n").encode()


def test_built_wheel_has_publishable_metadata_and_exact_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_backend, "_build_info", _clean_build_info)
    wheel = tmp_path / build_backend.build_wheel(str(tmp_path))
    verifier = _verifier()

    assert verifier.inspect_wheel(wheel, f"v{build_backend.VERSION}") == build_backend.VERSION

    with pytest.raises(verifier.DistributionVerificationError, match="does not match"):
        verifier.inspect_wheel(wheel, "v9.9.9")


def test_distribution_verifier_installs_cli_and_initializes_a_new_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_backend, "_build_info", _clean_build_info)
    wheel = tmp_path / build_backend.build_wheel(str(tmp_path))
    verifier = _verifier()

    version = verifier.inspect_wheel(wheel, f"v{build_backend.VERSION}")
    verifier.verify_install(wheel, version)

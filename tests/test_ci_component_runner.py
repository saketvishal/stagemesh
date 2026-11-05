from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from stagemesh.ci_validation_plan import COMPONENT, derive_ci_validation_plan

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_component_runner_script", ROOT / "scripts" / "run_ci_component_tests.py"
)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def write_plan(path: Path, source: str = "src/stagemesh/capabilities.py") -> dict[str, object]:
    payload = derive_ci_validation_plan((source,)).to_dict()
    assert payload["mode"] == COMPONENT
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_runner_loads_only_immutable_audited_targets(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    payload = write_plan(path)

    targets = runner.load_targets(path, "windows")

    assert targets == tuple(
        dict.fromkeys(
            (*payload["direct_test_targets"], *payload["dependent_test_targets"])
        )
    )
    assert targets


def test_runner_reports_an_unrequired_os_without_claiming_coverage(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    payload = write_plan(path)
    payload["required_os"] = ["linux"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert runner.load_targets(path, "windows") == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(contract="unsupported"),
        lambda payload: payload.update(mode="full"),
        lambda payload: payload.update(required_os=[]),
        lambda payload: payload.update(direct_test_targets=[]),
        lambda payload: payload.update(
            direct_test_targets=["tests/test_capability_registry.py; echo injected"]
        ),
        lambda payload: payload.update(direct_test_targets=["-k", "capability"]),
        lambda payload: payload.update(dependent_test_targets=["../outside.py"]),
    ],
)
def test_runner_fails_closed_for_malformed_or_unallowlisted_plan(
    tmp_path: Path, mutation
) -> None:
    path = tmp_path / "plan.json"
    payload = write_plan(path)
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        runner.load_targets(path, "windows")


def test_runner_invokes_pytest_with_argument_list_not_shell_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.json"
    payload = write_plan(path, "src/stagemesh/task_sources.py")
    calls: list[tuple[list[str], Path, bool]] = []

    def run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd, check))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", run)

    assert runner.main(["--plan", str(path), "--os", "linux"]) == 0
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *payload["direct_test_targets"],
                *payload["dependent_test_targets"],
            ],
            ROOT,
            False,
        )
    ]


def test_runner_refuses_symlink_plan(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    write_plan(target)
    link = tmp_path / "plan.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="non-symlink"):
        runner.load_targets(link, "linux")

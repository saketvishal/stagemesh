from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh import readiness
from stagemesh.persistence import Store
from stagemesh.runtime_repair import (
    RuntimeContamination,
    detect_runtime_contamination,
    plan_runtime_repair,
    project_runtime_venv,
    repair_runtime,
    venv_python,
)
from stagemesh.runtime_freshness import RuntimeFreshness


def _runtime(project: Path) -> tuple[Path, Path, Path]:
    venv = project_runtime_venv(project)
    site = venv / "Lib" / "site-packages"
    package = site / "stagemesh"
    package.mkdir(parents=True)
    py = venv_python(venv)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    return venv, site, package


def test_clean_installed_runtime_is_not_contaminated(tmp_path: Path) -> None:
    project = tmp_path / "product"
    project.mkdir()
    venv, _site, package = _runtime(project)

    report = detect_runtime_contamination(project, imported_package_path=package, environ={})
    plan = plan_runtime_repair(project, imported_package_path=package, environ={})

    assert report.imported_package_outside_project_runtime is False
    assert report.contaminated is False
    assert plan.contaminated is False
    assert plan.removals == ()
    assert plan.reinstall_command == ()
    assert plan.python == venv_python(venv)


def test_editable_install_pointing_outside_the_project_is_planned_for_backup_removal(tmp_path: Path) -> None:
    project = tmp_path / "product"
    project.mkdir()
    _venv, site, _package = _runtime(project)
    other = tmp_path / "stagemesh-source" / "src" / "stagemesh"
    other.mkdir(parents=True)
    pth = site / "__editable__.stagemesh.pth"
    pth.write_text(str(other.parent), encoding="utf-8")

    report = detect_runtime_contamination(project, imported_package_path=other, environ={})
    plan = plan_runtime_repair(project, imported_package_path=other, environ={})

    assert report.imported_package_outside_project_runtime is True
    assert report.next == "stagemesh runtime repair --write"
    assert plan.contaminated is True
    assert plan.removals == (pth,)


def test_pythonpath_contamination_is_reported_even_before_metadata_repair(tmp_path: Path) -> None:
    project = tmp_path / "product"
    project.mkdir()
    _venv, _site, package = _runtime(project)
    other_src = tmp_path / "other-checkout" / "src"
    (other_src / "stagemesh").mkdir(parents=True)

    report = detect_runtime_contamination(project, imported_package_path=package, environ={"PYTHONPATH": str(other_src)})

    assert report.imported_package_outside_project_runtime is False
    assert report.pythonpath_contaminated is True
    assert report.contaminated is True


def test_runtime_repair_dry_run_does_not_remove_metadata_or_reinstall(tmp_path: Path) -> None:
    project = tmp_path / "product"
    project.mkdir()
    _venv, site, _package = _runtime(project)
    other = tmp_path / "stagemesh-source" / "src" / "stagemesh"
    other.mkdir(parents=True)
    pth = site / "__editable__.stagemesh.pth"
    pth.write_text(str(other.parent), encoding="utf-8")

    plan = plan_runtime_repair(project, imported_package_path=other, environ={})

    assert plan.dry_run is True
    assert pth.exists()
    assert plan.reinstalled is False
    assert not (project / ".stagemesh" / "tooling" / "repair-backups").exists()


def test_runtime_repair_write_backs_up_editable_metadata_preserves_project_state_and_allows_clean_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "product"
    project.mkdir()
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    (runtime / "worktrees").mkdir()
    (runtime / "config.json").write_text('{"routing": {"require_independent_review": true}}', encoding="utf-8")
    (runtime / "contracts" / "T-1.json").write_text("{}", encoding="utf-8")
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    store.close()
    _venv, site, package = _runtime(project)
    other = tmp_path / "stagemesh-source"
    (other / "src" / "stagemesh").mkdir(parents=True)
    (other / "pyproject.toml").write_text('[project]\nname = "stagemesh"\n', encoding="utf-8")
    pth = site / "__editable__.stagemesh.pth"
    pth.write_text(str(other / "src"), encoding="utf-8")

    calls: list[list[str]] = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = repair_runtime(project, imported_package_path=other / "src" / "stagemesh", runner=fake_runner)

    assert result.reinstalled is True
    assert calls == [list(result.reinstall_command)]
    assert not pth.exists()
    assert result.backups and Path(result.backups[0]["backup"]).read_text(encoding="utf-8") == str(other / "src")
    assert (runtime / "config.json").exists()
    assert (runtime / "stagemesh.sqlite3").exists()
    assert (runtime / "contracts" / "T-1.json").exists()
    assert (runtime / "worktrees").exists()

    clean = RuntimeContamination(
        project_runtime=runtime,
        venv=project_runtime_venv(project),
        imported_package_path=package,
        imported_package_outside_project_runtime=False,
        pythonpath_entries=(),
        pythonpath_contaminated=False,
        next=None,
    )
    monkeypatch.setattr(cli_module, "detect_runtime_contamination", lambda project: clean)
    monkeypatch.setattr(readiness, "check_runtime", lambda project: RuntimeFreshness())

    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0

    text = capsys.readouterr().out
    assert f"imported package path: {package}" in text
    assert "imported package outside project runtime: False" in text
    assert "next: stagemesh runtime repair --write" not in text


def test_doctor_prints_runtime_contamination_and_repair_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "product"
    project.mkdir()
    Store(project / ".stagemesh" / "stagemesh.sqlite3").migrate()
    venv, _site, _package = _runtime(project)
    other = tmp_path / "other" / "src" / "stagemesh"
    other.mkdir(parents=True)
    contaminated = RuntimeContamination(
        project_runtime=project / ".stagemesh",
        venv=venv,
        imported_package_path=other,
        imported_package_outside_project_runtime=True,
        pythonpath_entries=(),
        pythonpath_contaminated=False,
        next="stagemesh runtime repair --write",
    )
    monkeypatch.setattr(cli_module, "detect_runtime_contamination", lambda project: contaminated)
    monkeypatch.setattr(readiness, "check_runtime", lambda project: RuntimeFreshness())

    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0

    text = capsys.readouterr().out
    assert f"imported package path: {other}" in text
    assert "imported package outside project runtime: True" in text
    assert "next: stagemesh runtime repair --write" in text

from __future__ import annotations

import json
from pathlib import Path

from build_coordinator.runtime_provenance import collect_runtime_provenance
from test_project_backlog import make_project_repo, register_project, stagemesh


def test_runtime_provenance_reports_control_plane_mismatch(monkeypatch, tmp_path):
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    (actual / "build_coordinator").mkdir(parents=True)
    package_init = actual / "build_coordinator" / "__init__.py"
    package_init.write_text("", encoding="utf-8")
    monkeypatch.setenv("STAGEMESH_CONTROL_PLANE_ROOT", str(expected))

    report = collect_runtime_provenance(package_root=package_init).as_dict()

    assert report["package_path"] == str(actual.resolve())
    assert report["conflicts"][0]["code"] == "STAGEMESH_CONTROL_PLANE_MISMATCH"
    assert report["conflicts"][0]["expected"] == str(expected.resolve())


def test_runtime_provenance_reports_conflicting_importable_package(monkeypatch, tmp_path):
    stale = tmp_path / "stale"
    (stale / "build_coordinator").mkdir(parents=True)
    (stale / "build_coordinator" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(stale))

    report = collect_runtime_provenance().as_dict()

    conflicts = {item["code"]: item for item in report["conflicts"]}
    assert conflicts["STAGEMESH_CONFLICTING_INSTALLATION"]["actual"] == str(stale.resolve())


def test_project_status_includes_runtime_provenance(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"P-1": {"review": "NONE"}}, concurrency=1)
    register_project(root)

    proc = stagemesh(
        ["project", "status", "fixture"],
        cwd=tmp_path,
        registry=registry,
        extra_env={"STAGEMESH_TEST_STATE_GUARD": "0"},
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    provenance = payload["runtime_provenance"]
    assert provenance["controller_python"]
    assert provenance["package_path"]
    assert provenance["project_root"] == str(root.resolve())
    assert provenance["coordinator_database"].endswith("coordinator.sqlite3")

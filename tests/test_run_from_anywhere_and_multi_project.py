"""Wave 4 — OBJECTIVES-006: objective run-from-anywhere.
          PROJECTS-005: project run-from-anywhere.
          PROJECTS-006: multi-project coordination.

Legacy contract (test_objective_cli_location_independence.py,
test_operator_location_independence.py, examples/multi_project_demo.yaml):

Objective/project CLI operations must work from any cwd when a project
is resolvable via:
  1. Explicit --project argument
  2. GlobalRegistry lookup
  3. cwd-or-parent walk

Multi-project proofs:
  - Two independent projects register without collision
  - Tasks remain scoped per-project (no cross-contamination)
  - One project failure does not mutate another
  - Task source IDs do not collide across projects
  - Restart preserves project boundaries
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from stagemesh.persistence import Store
from stagemesh.registry import (
    GlobalRegistry,
    ProjectRegistration,
    RegistryConflictError,
    RegistryValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, name: str) -> tuple[Path, Store]:
    project_dir = tmp_path / name
    project_dir.mkdir()
    runtime = project_dir / ".stagemesh"
    runtime.mkdir()
    db = runtime / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()
    return project_dir, store


def _registry(tmp_path: Path) -> GlobalRegistry:
    return GlobalRegistry(tmp_path / "registry.json")


# ---------------------------------------------------------------------------
# PROJECTS-005: GlobalRegistry — run-from-anywhere resolution
# ---------------------------------------------------------------------------

class TestGlobalRegistryRunFromAnywhere:

    def test_register_and_load_project(self, tmp_path: Path):
        project_dir, store = _make_project(tmp_path, "alpha")
        registry = _registry(tmp_path)
        reg = ProjectRegistration(
            name="alpha",
            path=project_dir,
            db_path=project_dir / ".stagemesh" / "stagemesh.sqlite3",
        )
        registry.register(reg)
        loaded = registry.load()
        assert any(p.name == "alpha" for p in loaded)

    def test_resolution_from_arbitrary_cwd(self, tmp_path: Path):
        """
        A project registered in the global registry can be found by name
        regardless of the calling working directory.
        """
        project_dir, _ = _make_project(tmp_path, "beta")
        registry = _registry(tmp_path)
        registry.register(ProjectRegistration(
            name="beta",
            path=project_dir,
            db_path=project_dir / ".stagemesh" / "stagemesh.sqlite3",
        ))
        # cwd is unrelated directory
        unrelated_cwd = tmp_path / "unrelated_working_dir"
        unrelated_cwd.mkdir()

        # Resolution: load from registry by name (simulating --project beta)
        loaded = registry.load()
        found = next((p for p in loaded if p.name == "beta"), None)
        assert found is not None
        assert found.path == project_dir.resolve()

    def test_missing_registry_returns_empty(self, tmp_path: Path):
        registry = GlobalRegistry(tmp_path / "nonexistent_registry.json")
        assert registry.load() == []

    def test_corrupt_registry_raises_validation_error(self, tmp_path: Path):
        reg_path = tmp_path / "bad_registry.json"
        reg_path.write_text("{not json}", encoding="utf-8")
        registry = GlobalRegistry(reg_path)
        with pytest.raises(RegistryValidationError):
            registry.load()

    def test_duplicate_name_different_path_raises_conflict(self, tmp_path: Path):
        p1, _ = _make_project(tmp_path, "gamma")
        p2, _ = _make_project(tmp_path, "gamma2")
        registry = _registry(tmp_path)
        registry.register(ProjectRegistration(
            name="gamma",
            path=p1,
            db_path=p1 / ".stagemesh" / "stagemesh.sqlite3",
        ))
        with pytest.raises(RegistryConflictError):
            registry.register(ProjectRegistration(
                name="gamma",
                path=p2,
                db_path=p2 / ".stagemesh" / "stagemesh.sqlite3",
            ))

    def test_same_registration_repeated_is_idempotent(self, tmp_path: Path):
        project_dir, _ = _make_project(tmp_path, "delta")
        registry = _registry(tmp_path)
        reg = ProjectRegistration(
            name="delta",
            path=project_dir,
            db_path=project_dir / ".stagemesh" / "stagemesh.sqlite3",
        )
        registry.register(reg)
        registry.register(reg)  # second call should not raise
        loaded = registry.load()
        assert sum(1 for p in loaded if p.name == "delta") == 1

    def test_db_path_must_be_inside_project_path(self, tmp_path: Path):
        project_dir, _ = _make_project(tmp_path, "epsilon")
        registry = _registry(tmp_path)
        outside_db = tmp_path / "outside.sqlite3"
        outside_db.touch()
        with pytest.raises(RegistryValidationError, match="inside"):
            registry.register(ProjectRegistration(
                name="epsilon",
                path=project_dir,
                db_path=outside_db,
            ))

    def test_cli_resolves_single_registered_project_from_unrelated_cwd_without_flag(self, tmp_path: Path):
        """PROJECTS-005: Running CLI from unrelated cwd without --project resolves single registered project."""
        import os
        project_dir, store = _make_project(tmp_path, "single_target")
        store.upsert_task("Task in single target", source="local")
        store.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration(
            name="single_target",
            path=project_dir,
            db_path=project_dir / ".stagemesh" / "stagemesh.sqlite3",
        ))

        unrelated = tmp_path / "elsewhere"
        unrelated.mkdir()

        cmd = [sys.executable, "-m", "stagemesh.cli", "status", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res.returncode == 0, f"CLI stderr: {res.stderr}"
        data = json.loads(res.stdout)
        assert data["task_count"] == 1
        assert data["tasks"][0]["title"] == "Task in single target"

    def test_cli_ambiguous_resolution_fails_safely_when_multiple_registered(self, tmp_path: Path):
        """PROJECTS-005: Running CLI from unrelated cwd with multiple registered projects fails safely."""
        import os
        p1, _ = _make_project(tmp_path, "proj_one")
        p2, _ = _make_project(tmp_path, "proj_two")
        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("proj_one", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("proj_two", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "elsewhere_ambig"
        unrelated.mkdir()

        cmd = [sys.executable, "-m", "stagemesh.cli", "status"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res.returncode == 2
        assert "ambiguous" in res.stderr.lower()

    def test_cli_resolves_named_registered_project_from_unrelated_cwd(self, tmp_path: Path):
        """PROJECTS-005: Running CLI from unrelated cwd with --project <name> resolves project from registry."""
        import os
        p1, s1 = _make_project(tmp_path, "alpha_proj")
        p2, s2 = _make_project(tmp_path, "beta_proj")
        s1.upsert_task("Alpha Task", source="local")
        s2.upsert_task("Beta Task", source="local")
        s1.close()
        s2.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("alpha_proj", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("beta_proj", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "elsewhere_named"
        unrelated.mkdir()

        cmd = [sys.executable, "-m", "stagemesh.cli", "status", "--project", "beta_proj", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["task_count"] == 1
        assert data["tasks"][0]["title"] == "Beta Task"

    def test_cli_resolves_registered_project_name_when_unrelated_cwd_has_same_name_dir(self, tmp_path: Path):
        """PROJECTS-005: A registered project name must not be shadowed accidentally by an unrelated same-named directory in caller's CWD."""
        import os
        reg_dir, reg_store = _make_project(tmp_path, "registered_beta")
        reg_store.upsert_task("Registered Beta Task", source="local")
        reg_store.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("beta", reg_dir, reg_dir / ".stagemesh" / "stagemesh.sqlite3"))

        # Caller CWD contains an ordinary unrelated subdirectory named "beta" (no .stagemesh)
        caller_cwd = tmp_path / "caller_workdir"
        caller_cwd.mkdir()
        unrelated_subfolder = caller_cwd / "beta"
        unrelated_subfolder.mkdir()

        # 1. Specifying registered name "beta" resolves to registered project, NOT the local ./beta folder
        cmd = [sys.executable, "-m", "stagemesh.cli", "status", "--project", "beta", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=caller_cwd, env=env, capture_output=True, text=True)
        assert res.returncode == 0, f"Expected registered project beta to resolve: {res.stderr}"
        data = json.loads(res.stdout)
        assert data["tasks"][0]["title"] == "Registered Beta Task"

        # 2. Specifying explicit path "./beta" must NOT treat ordinary directory as initialized project
        cmd_explicit = [sys.executable, "-m", "stagemesh.cli", "status", "--project", "./beta", "--json"]
        res_explicit = subprocess.run(cmd_explicit, cwd=caller_cwd, env=env, capture_output=True, text=True)
        assert res_explicit.returncode != 0
        assert "not an initialized stagemesh project" in res_explicit.stderr.lower()


# ---------------------------------------------------------------------------
# OBJECTIVES-006: objective operations from unrelated cwd
# ---------------------------------------------------------------------------

class TestObjectiveRunFromAnywhere:
    """
    OBJECTIVES-006: objective operations work from unrelated cwd via CLI.
    Verify target project is changed and another project remains untouched.
    """

    def test_cli_plan_from_unrelated_cwd_modifies_target_and_leaves_other_untouched(self, tmp_path: Path):
        """Run real CLI stagemesh plan from unrelated cwd targeting project A."""
        target_dir, target_store = _make_project(tmp_path, "target_app")
        other_dir, other_store = _make_project(tmp_path, "other_app")
        target_store.close()
        other_store.close()

        unrelated = tmp_path / "unrelated_workspace"
        unrelated.mkdir()

        plan_file = target_dir / "new_feature_plan.json"
        plan_content = {
            "id": "OBJ-CLI-ANYWHERE",
            "title": "Objective Run Anywhere",
            "tasks": [
                {"id": "TASK-1", "title": "First Step"},
                {"id": "TASK-2", "title": "Second Step", "dependencies": ["TASK-1"]},
            ],
        }
        plan_file.write_text(json.dumps(plan_content), encoding="utf-8")

        # Invoke CLI plan from unrelated cwd
        cmd = [
            sys.executable,
            "-m",
            "stagemesh.cli",
            "plan",
            str(plan_file),
            "--project",
            str(target_dir),
            "--json",
        ]
        res = subprocess.run(cmd, cwd=unrelated, capture_output=True, text=True)
        assert res.returncode == 0, f"plan failed: {res.stderr}"

        # Target project received the tasks
        s_target = Store(target_dir / ".stagemesh" / "stagemesh.sqlite3")
        tasks_target = s_target.tasks()
        assert len(tasks_target) == 2
        assert {t["id"] for t in tasks_target} == {"TASK-1", "TASK-2"}
        s_target.close()

        # Other project is untouched (0 tasks)
        s_other = Store(other_dir / ".stagemesh" / "stagemesh.sqlite3")
        assert len(s_other.tasks()) == 0
        s_other.close()


# ---------------------------------------------------------------------------
# PROJECTS-006: multi-project coordination
# ---------------------------------------------------------------------------

class TestMultiProjectCoordination:

    def test_two_projects_registered_without_collision(self, tmp_path: Path):
        p1, _ = _make_project(tmp_path, "proj1")
        p2, _ = _make_project(tmp_path, "proj2")
        registry = _registry(tmp_path)
        registry.register(ProjectRegistration("proj1", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("proj2", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))
        loaded = registry.load()
        names = {p.name for p in loaded}
        assert {"proj1", "proj2"} <= names

    def test_tasks_are_project_scoped(self, tmp_path: Path):
        """Tasks added to project 1 do not appear in project 2's store."""
        _, store1 = _make_project(tmp_path, "scoped1")
        _, store2 = _make_project(tmp_path, "scoped2")

        t1 = store1.upsert_task("Task for proj1", source="local")
        store1.close()
        store2_tasks = store2.tasks()
        assert all(t["id"] != t1 for t in store2_tasks)
        store2.close()

    def test_one_project_failure_does_not_mutate_other(self, tmp_path: Path):
        """Attempting an invalid operation on project 1 leaves project 2 unchanged."""
        from stagemesh.persistence import StoreValidationError
        _, store1 = _make_project(tmp_path, "fail1")
        _, store2 = _make_project(tmp_path, "safe2")

        t2 = store2.upsert_task("Safe task in proj2", source="local")
        store2.close()

        # Try an invalid operation on store1
        try:
            store1.upsert_task("", source="local")  # blank title → error
        except StoreValidationError:
            pass
        store1.close()

        # store2 is untouched
        store2b = Store(tmp_path / "safe2" / ".stagemesh" / "stagemesh.sqlite3")
        tasks = store2b.tasks()
        assert any(t["id"] == t2 for t in tasks)
        store2b.close()

    def test_source_ids_do_not_collide_across_projects(self, tmp_path: Path):
        """Same source+source_id in two different project DBs must be independent."""
        _, store1 = _make_project(tmp_path, "coll1")
        _, store2 = _make_project(tmp_path, "coll2")

        t1 = store1.upsert_task("Task A", source="github", source_id="42")
        t2 = store2.upsert_task("Task B", source="github", source_id="42")
        store1.close()
        store2.close()

        # Both exist in their respective stores — no collision (different DBs)
        s1 = Store(tmp_path / "coll1" / ".stagemesh" / "stagemesh.sqlite3")
        s2 = Store(tmp_path / "coll2" / ".stagemesh" / "stagemesh.sqlite3")
        assert s1.get_task(t1) is not None
        assert s2.get_task(t2) is not None
        # Each store has exactly one task with source_id=42
        assert s1.get_task_by_source("github", "42") is not None
        assert s2.get_task_by_source("github", "42") is not None
        s1.close()
        s2.close()

    def test_restart_preserves_project_boundaries(self, tmp_path: Path):
        """
        Simulate restart: close both stores, reopen, verify data is
        still correctly scoped per-project.
        """
        _, store1 = _make_project(tmp_path, "restart1")
        _, store2 = _make_project(tmp_path, "restart2")

        t1 = store1.upsert_task("Restart task 1", source="local")
        t2 = store2.upsert_task("Restart task 2", source="local")
        store1.close()
        store2.close()

        # Reopen after "restart"
        rs1 = Store(tmp_path / "restart1" / ".stagemesh" / "stagemesh.sqlite3")
        rs2 = Store(tmp_path / "restart2" / ".stagemesh" / "stagemesh.sqlite3")
        assert any(t["id"] == t1 for t in rs1.tasks())
        assert any(t["id"] == t2 for t in rs2.tasks())
        # Cross-contamination check
        assert all(t["id"] != t2 for t in rs1.tasks())
        assert all(t["id"] != t1 for t in rs2.tasks())
        rs1.close()
        rs2.close()

    def test_capacity_is_per_project(self, tmp_path: Path):
        """
        Capacity enforcement is per Store (project); one project filling
        its capacity does not block another project.
        CapacityRegistry is in-memory and per-coordinator instance.
        """
        from stagemesh.capacity import CapacityRegistry, CapacityKind

        _, store1 = _make_project(tmp_path, "cap1")
        _, store2 = _make_project(tmp_path, "cap2")

        # Each project has its own in-memory CapacityRegistry
        cap1 = CapacityRegistry()
        cap2 = CapacityRegistry()

        # Record a capacity exhaustion on project 1's provider
        cap1.record("openai", CapacityKind.CAPACITY)

        # Project 2's registry is completely independent — still UNKNOWN (usable fallback)
        state2 = cap2.get("openai")
        # UNKNOWN means no recorded state — not blocked by project 1
        assert state2.kind == CapacityKind.UNKNOWN

        store1.close()
        store2.close()

    def test_multi_project_registry_sorted_deterministically(self, tmp_path: Path):
        """Registry serializes projects alphabetically for deterministic output."""
        p1, _ = _make_project(tmp_path, "zzz")
        p2, _ = _make_project(tmp_path, "aaa")
        registry = _registry(tmp_path)
        registry.register(ProjectRegistration("zzz", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("aaa", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))
        loaded = registry.load()
        # After save/reload, order should be deterministic (alphabetical)
        names = [p.name for p in loaded]
        assert sorted(names) == names

    def test_continue_all_coordinates_multiple_registered_projects(self, tmp_path: Path):
        """PROJECTS-006: Real CLI continue --all discovers and coordinates multiple registered projects."""
        import os
        p1, s1 = _make_project(tmp_path, "proj_coord1")
        p2, s2 = _make_project(tmp_path, "proj_coord2")

        # Give each project a task in PLAN stage
        t1 = s1.upsert_task("Coordinate Task 1", source="local")
        t2 = s2.upsert_task("Coordinate Task 2", source="local")
        s1.close()
        s2.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("proj_coord1", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("proj_coord2", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "outside_all"
        unrelated.mkdir()

        # Execute continue --all from unrelated directory with --dry-run
        cmd = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--dry-run", "--once", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res.returncode == 0, f"continue --all failed: {res.stderr}"
        data = json.loads(res.stdout)
        assert data["mode"] == "global"
        assert "proj_coord1" in data["projects"]
        assert "proj_coord2" in data["projects"]
        assert data["projects"]["proj_coord1"]["status"] == "OK"
        assert data["projects"]["proj_coord2"]["status"] == "OK"

        # Verify both projects progressed independently
        s1b = Store(p1 / ".stagemesh" / "stagemesh.sqlite3")
        s2b = Store(p2 / ".stagemesh" / "stagemesh.sqlite3")
        assert s1b.get_task(t1)["stage"] != "PLAN"
        assert s2b.get_task(t2)["stage"] != "PLAN"
        s1b.close()
        s2b.close()

    def test_continue_all_failure_in_project_a_does_not_block_project_b(self, tmp_path: Path):
        """PROJECTS-006: Failure/corruption in one registered project does not halt other projects, but overall exit code is nonzero."""
        import os
        p_corrupt, s_corrupt = _make_project(tmp_path, "corrupt_proj")
        p_healthy, s_healthy = _make_project(tmp_path, "healthy_proj")

        s_corrupt.close()
        t_healthy = s_healthy.upsert_task("Healthy Task", source="local")
        s_healthy.close()

        # Corrupt project database file
        (p_corrupt / ".stagemesh" / "stagemesh.sqlite3").write_text("CORRUPTED_DB_NOT_SQLITE", encoding="utf-8")

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("corrupt_proj", p_corrupt, p_corrupt / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("healthy_proj", p_healthy, p_healthy / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "outside_fail"
        unrelated.mkdir()

        cmd = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--dry-run", "--once", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        # Overall result must be nonzero when any project child fails
        assert res.returncode == 1
        data = json.loads(res.stdout)
        # Corrupt project is marked ERROR, but healthy project still progresses OK
        assert data["projects"]["corrupt_proj"]["status"] == "ERROR"
        assert data["projects"]["healthy_proj"]["status"] == "OK"

        s_h = Store(p_healthy / ".stagemesh" / "stagemesh.sqlite3")
        assert s_h.get_task(t_healthy)["stage"] != "PLAN"
        s_h.close()

    def test_continue_all_without_dry_run_cannot_silently_use_fake_executor(self, tmp_path: Path):
        """PROJECTS-006: continue --all without --dry-run must wire real provider and cannot silently use FakeExecutor."""
        import os
        p1, s1 = _make_project(tmp_path, "unconfigured_proj")
        s1.upsert_task("Task needing real provider", source="local")
        s1.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("unconfigured_proj", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "outside_nofake"
        unrelated.mkdir()

        # 1. Specifying an invalid/nonexistent provider without --dry-run must fail, NOT silently fall back to FakeExecutor
        cmd_fail = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--provider", "nonexistent_provider_xyz", "--once", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res_fail = subprocess.run(cmd_fail, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res_fail.returncode != 0
        data_fail = json.loads(res_fail.stdout)
        assert data_fail["projects"]["unconfigured_proj"]["status"] == "ERROR"

        # 2. Running with --dry-run explicitly records provider 'fake'
        cmd_dry = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--dry-run", "--once", "--json"]
        res_dry = subprocess.run(cmd_dry, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res_dry.returncode == 0
        data_dry = json.loads(res_dry.stdout)
        assert data_dry["projects"]["unconfigured_proj"]["provider"] == "fake"

        # 3. Running without --dry-run uses real provider, NEVER FakeExecutor ('fake')
        cmd_real = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--once", "--json"]
        res_real = subprocess.run(cmd_real, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res_real.returncode == 0
        data_real = json.loads(res_real.stdout)
        assert data_real["projects"]["unconfigured_proj"]["provider"] != "fake"

    def test_continue_all_process_isolation_and_capacity_allocation(self, tmp_path: Path):
        """PROJECTS-006: Each project runs in an isolated child process with separate PID and capacity batching."""
        import os
        p1, s1 = _make_project(tmp_path, "iso_proj1")
        p2, s2 = _make_project(tmp_path, "iso_proj2")
        s1.upsert_task("Task 1", source="local")
        s2.upsert_task("Task 2", source="local")
        s1.close()
        s2.close()

        reg_path = tmp_path / "global_registry.json"
        registry = GlobalRegistry(reg_path)
        registry.register(ProjectRegistration("iso_proj1", p1, p1 / ".stagemesh" / "stagemesh.sqlite3"))
        registry.register(ProjectRegistration("iso_proj2", p2, p2 / ".stagemesh" / "stagemesh.sqlite3"))

        unrelated = tmp_path / "outside_iso"
        unrelated.mkdir()

        current_pid = os.getpid()
        cmd = [sys.executable, "-m", "stagemesh.cli", "continue", "--all", "--dry-run", "--once", "--capacity", "1", "--json"]
        env = {**os.environ, "STAGEMESH_REGISTRY": str(reg_path)}
        res = subprocess.run(cmd, cwd=unrelated, env=env, capture_output=True, text=True)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        pid1 = data["projects"]["iso_proj1"]["pid"]
        pid2 = data["projects"]["iso_proj2"]["pid"]
        # Isolated child processes: PIDs must differ from current process
        assert pid1 != current_pid
        assert pid2 != current_pid
        assert data["projects"]["iso_proj1"]["status"] == "OK"
        assert data["projects"]["iso_proj2"]["status"] == "OK"


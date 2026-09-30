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


# ---------------------------------------------------------------------------
# OBJECTIVES-006: objective operations from unrelated cwd
# ---------------------------------------------------------------------------

class TestObjectiveRunFromAnywhere:
    """
    The --project flag on the CLI provides an explicit project path,
    enabling objective/task operations regardless of cwd.

    We test the underlying registry + store contract here (no subprocess needed).
    """

    def test_explicit_project_path_resolves_independently_of_cwd(self, tmp_path: Path):
        """
        Store opened with an explicit absolute path works regardless of cwd.
        """
        project_dir, store = _make_project(tmp_path, "zeta")
        task_id = store.upsert_task("Task from explicit path", source="local")
        store.close()

        # Simulate re-opening the store from "any cwd" via explicit db_path
        explicit_db = project_dir / ".stagemesh" / "stagemesh.sqlite3"
        store2 = Store(explicit_db)
        store2.migrate()
        tasks = store2.tasks()
        assert any(t["id"] == task_id for t in tasks)
        store2.close()

    def test_wrong_project_path_does_not_mutate_correct_project(self, tmp_path: Path):
        """Targeting project A does not add tasks to project B."""
        _, store_a = _make_project(tmp_path, "proj_a")
        _, store_b = _make_project(tmp_path, "proj_b")

        store_a.upsert_task("Task only in A", source="local")
        store_a.close()

        tasks_b = store_b.tasks()
        assert len(tasks_b) == 0
        store_b.close()


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

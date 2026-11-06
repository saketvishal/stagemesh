"""Independent adversarial acceptance tests for the StageMesh test architecture redesign.

Covers:
- Unknown, duplicate, conflicting, and malformed tier classifications (fail-closed).
- Parallel safety and isolation contract enforcement.
- Hermetic filesystem, database, Git, port, and environment isolation.
- Hung and orphaned subprocess detection and process tree cleanup.
- Lane timeouts and cancellation cleanup.
- Append-only journal validation and strict identity binding.
- Rejection of stale resume journals (mismatched commit, catalog, policy, interpreter, OS, deps).
- Safe resume of matching passed lanes without re-execution.
- Windows path normalization, file locking, and readonly cleanup.
- Preservation of component-planner fail-closed escalation boundaries.
- Exact catalog bijection against collected test suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stagemesh.ci_validation_plan import COMPONENT, FULL, derive_ci_validation_plan
from stagemesh.regression_orchestrator import (
    Lane,
    LaneResult,
    PytestDuration,
    RegressionOrchestrator,
    RunIdentity,
    build_default_lanes,
    parse_collected_nodeids,
    parse_pytest_counts,
    parse_pytest_durations,
)
from stagemesh.test_isolation import (
    RegressionRunLock,
    WorkerPoolIsolation,
    kill_process_tree,
)
from stagemesh.test_taxonomy import (
    FILE_TIER_MAP,
    TIER_ISOLATION_CONTRACTS,
    TestTier,
    catalog_digest,
    normalize_test_path,
    policy_fingerprint,
    tier_for_test,
    validate_catalog,
)


def test_unknown_tier_classification_fails_closed() -> None:
    """An unknown test file not listed in the catalog must fail closed."""
    fake_test = "tests/test_unclassified_mystery.py::test_something"
    with pytest.raises(ValueError, match="Unclassified test in taxonomy"):
        tier_for_test(fake_test)

    # In collection validation, it must also fail closed
    with pytest.raises(ValueError, match="lack tier classification"):
        validate_catalog([fake_test], allow_uncollected_catalog_entries=True)


def test_duplicate_classification_fails_closed() -> None:
    """A duplicate collected nodeid must not be silently overwritten."""
    nodeid = "tests/test_attribution_guards.py::test_one"
    with pytest.raises(ValueError, match="Duplicate collected test nodeid"):
        validate_catalog([nodeid, nodeid], allow_uncollected_catalog_entries=True)


def test_malformed_catalog_entries_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalog entries outside tests/ or with invalid tiers must fail closed."""
    assert normalize_test_path("tests\\test_foo.py::test_bar") == "tests/test_foo.py::test_bar"
    monkeypatch.setitem(FILE_TIER_MAP, "outside_tests.py", TestTier.DETERMINISTIC_UNIT)
    with pytest.raises(ValueError, match="Malformed catalog file path"):
        validate_catalog([], allow_uncollected_catalog_entries=True)


def test_invalid_catalog_tier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(FILE_TIER_MAP, "tests/test_invalid_tier.py", "not-a-tier")
    with pytest.raises(TypeError, match="Invalid tier"):
        validate_catalog([], allow_uncollected_catalog_entries=True)


def test_malformed_collected_nodeid_fails_closed() -> None:
    with pytest.raises(ValueError, match="Malformed collected test nodeid"):
        validate_catalog(["tests/test_attribution_guards.py"], allow_uncollected_catalog_entries=True)


def test_isolation_contracts_declare_parallel_safety_and_serialization() -> None:
    """Verify isolation contracts explicitly state parallel safety and reasons for serialization."""
    for tier in TestTier:
        contract = TIER_ISOLATION_CONTRACTS[tier]
        assert contract.tier == tier
        assert isinstance(contract.parallel_safe, bool)
        assert isinstance(contract.timeout_seconds, float)
        assert contract.timeout_seconds > 0.0
        assert len(contract.rationale) > 20

    # native_platform must be explicitly serialized
    assert TIER_ISOLATION_CONTRACTS[TestTier.NATIVE_PLATFORM].parallel_safe is False
    assert "native Windows" in TIER_ISOLATION_CONTRACTS[TestTier.NATIVE_PLATFORM].rationale

    # unit and component must be parallel safe
    assert TIER_ISOLATION_CONTRACTS[TestTier.DETERMINISTIC_UNIT].parallel_safe is True
    assert TIER_ISOLATION_CONTRACTS[TestTier.COMPONENT_CONTRACT].parallel_safe is True
    assert TIER_ISOLATION_CONTRACTS[TestTier.INTEGRATION_PROCESS].parallel_safe is True


def test_worker_filesystem_and_env_isolation(tmp_path: Path) -> None:
    """Parallel workers receive completely hermetic directories, ports, and sanitized envs."""
    pool = WorkerPoolIsolation(root_dir=tmp_path)
    try:
        ctx0 = pool.get_worker_context(0)
        ctx1 = pool.get_worker_context(1)

        # Worker directories must not overlap
        assert ctx0.worker_dir != ctx1.worker_dir
        assert ctx0.tmp_dir != ctx1.tmp_dir
        assert ctx0.home_dir != ctx1.home_dir
        assert ctx0.userprofile_dir != ctx1.userprofile_dir
        assert ctx0.database_dir != ctx1.database_dir
        assert ctx0.git_root != ctx1.git_root
        assert ctx0.worktree_root != ctx1.worktree_root
        assert ctx0.git_config_path != ctx1.git_config_path

        # Temporary directory environment variables must point to worker-isolated root
        assert ctx0.env["TMPDIR"] == str(ctx0.tmp_dir)
        assert ctx0.env["TEMP"] == str(ctx0.tmp_dir)
        assert ctx0.env["TMP"] == str(ctx0.tmp_dir)
        assert ctx1.env["TMPDIR"] == str(ctx1.tmp_dir)

        # Port bases must be distinct
        assert ctx0.port_base != ctx1.port_base
        assert ctx0.env["STAGEMESH_TEST_PORT_BASE"] != ctx1.env["STAGEMESH_TEST_PORT_BASE"]

        # Ambient provider credentials must be absent
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GITHUB_TOKEN"):
            assert key not in ctx0.env
            assert key not in ctx1.env

        # Git environment is hermetic
        assert ctx0.env["HOME"] == str(ctx0.home_dir)
        assert ctx0.env["GIT_CONFIG_GLOBAL"] == str(ctx0.git_config_path)
        assert ctx0.env["STAGEMESH_TEST_DATABASE_ROOT"] == str(ctx0.database_dir)
        assert ctx0.env["STAGEMESH_TEST_GIT_ROOT"] == str(ctx0.git_root)
        assert ctx0.env["STAGEMESH_TEST_WORKTREE_ROOT"] == str(ctx0.worktree_root)
    finally:
        pool.cleanup()


def test_worker_cleanup_refuses_target_outside_pool(tmp_path: Path) -> None:
    pool = WorkerPoolIsolation(root_dir=tmp_path / "pool")
    context = pool.get_worker_context(0)
    original_worker_dir = context.worker_dir
    unsafe_target = tmp_path / "do-not-delete"
    unsafe_target.mkdir()
    context.worker_dir = unsafe_target
    try:
        with pytest.raises(ValueError, match="refusing unsafe worker cleanup target"):
            context.cleanup()
        assert unsafe_target.exists()
    finally:
        context.worker_dir = original_worker_dir
        pool.cleanup()


def test_repository_run_lock_rejects_overlapping_regressions(tmp_path: Path) -> None:
    first = RegressionRunLock(tmp_path)
    second = RegressionRunLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another StageMesh regression run"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_hung_and_orphaned_subprocess_cleanup() -> None:
    """Hung processes and their process trees must be cleanly terminated."""
    code = "import time; time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    pid = proc.pid
    assert proc.poll() is None

    # Kill process tree
    kill_process_tree(pid)

    # Process must exit promptly
    exited = False
    for _ in range(50):
        if proc.poll() is not None:
            exited = True
            break
        time.sleep(0.05)
    assert exited is True, f"Process {pid} failed to terminate after kill_process_tree"


def test_lane_timeout_terminates_process_and_marks_failure(tmp_path: Path) -> None:
    """A lane that exceeds its deadline is aborted with timeout status and code 124."""
    slow_script = tmp_path / "test_slow_synthetic.py"
    slow_script.write_text("import time\ndef test_hung():\n    time.sleep(60)\n", encoding="utf-8")

    lane = Lane(
        lane_id="lane-timeout-test",
        tier=TestTier.INTEGRATION_PROCESS,
        parallel_safe=True,
        files=(str(slow_script),),
        timeout_seconds=0.8,
    )

    orchestrator = RegressionOrchestrator(lanes=(lane,), jobs=1, repo_root=tmp_path)
    res = orchestrator._execute_lane(lane, worker_id=0)

    assert res.passed is False
    assert res.timed_out is True
    assert res.returncode == 124
    assert "timeout" in res.error_message.lower()


def test_stale_resume_refused_on_mismatched_identity(tmp_path: Path) -> None:
    """Resume must fail closed when any aspect of the run identity changes."""
    journal_file = tmp_path / "test_journal.jsonl"
    current_identity = RunIdentity.current()

    # 1. Mismatched commit SHA
    bad_commit_id = RunIdentity(
        commit_sha="0000000000000000000000000000000000000000",
        catalog_digest=current_identity.catalog_digest,
        policy_fingerprint=current_identity.policy_fingerprint,
        interpreter=current_identity.interpreter,
        python_version=current_identity.python_version,
        operating_system=current_identity.operating_system,
        os_release=current_identity.os_release,
        dependencies_hash=current_identity.dependencies_hash,
    )
    journal_file.write_text(
        json.dumps({"event": "run_initialized", "identity": bad_commit_id.to_dict()}) + "\n",
        encoding="utf-8",
    )
    orch = RegressionOrchestrator(journal_path=journal_file, resume=True)
    with pytest.raises(ValueError, match="Resume refused: journal identity does not match"):
        orch._validate_or_init_journal()

    # 2. Mismatched catalog digest
    bad_catalog_id = RunIdentity(
        commit_sha=current_identity.commit_sha,
        catalog_digest="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        policy_fingerprint=current_identity.policy_fingerprint,
        interpreter=current_identity.interpreter,
        python_version=current_identity.python_version,
        operating_system=current_identity.operating_system,
        os_release=current_identity.os_release,
        dependencies_hash=current_identity.dependencies_hash,
    )
    journal_file.write_text(
        json.dumps({"event": "run_initialized", "identity": bad_catalog_id.to_dict()}) + "\n",
        encoding="utf-8",
    )
    orch = RegressionOrchestrator(journal_path=journal_file, resume=True)
    with pytest.raises(ValueError, match="catalog_digest"):
        orch._validate_or_init_journal()

    # 3. Mismatched OS
    bad_os_id = RunIdentity(
        commit_sha=current_identity.commit_sha,
        catalog_digest=current_identity.catalog_digest,
        policy_fingerprint=current_identity.policy_fingerprint,
        interpreter=current_identity.interpreter,
        python_version=current_identity.python_version,
        operating_system="FreeBSD",
        os_release=current_identity.os_release,
        dependencies_hash=current_identity.dependencies_hash,
    )
    journal_file.write_text(
        json.dumps({"event": "run_initialized", "identity": bad_os_id.to_dict()}) + "\n",
        encoding="utf-8",
    )
    orch = RegressionOrchestrator(journal_path=journal_file, resume=True)
    with pytest.raises(ValueError, match="operating_system"):
        orch._validate_or_init_journal()


def test_safe_resume_skips_matching_passed_lanes(tmp_path: Path) -> None:
    """When run identity matches exactly, already passed lanes are skipped and not re-executed."""
    journal_file = tmp_path / "resume_journal.jsonl"
    current_identity = RunIdentity.current()

    lane1 = Lane("unit-01", TestTier.DETERMINISTIC_UNIT, True, ("tests/test_docs_links.py",), 60.0)
    lane2 = Lane("unit-02", TestTier.DETERMINISTIC_UNIT, True, ("tests/test_readiness.py",), 60.0)
    orch = RegressionOrchestrator(lanes=(lane1, lane2), journal_path=journal_file, resume=True)

    # Write initial header and one passed lane
    journal_file.write_text(
        json.dumps(
            {
                "event": "run_initialized",
                "identity": current_identity.to_dict(),
                "execution_plan_sha256": orch._execution_plan_digest((lane1, lane2)),
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "lane_completed",
                "lane_id": "unit-01",
                "passed": True,
                "returncode": 0,
                "passed_count": 13,
                "failed_count": 0,
                "skipped_count": 0,
                "subtests_passed": 0,
                "test_durations": [
                    {
                        "nodeid": "tests/test_docs_links.py::test_docs_links",
                        "phase": "call",
                        "duration_seconds": 0.25,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    passed_lanes = orch._validate_or_init_journal()
    assert passed_lanes == {"unit-01"}
    assert orch._resumed_records["unit-01"]["passed_count"] == 13
    assert orch._resumed_records["unit-01"]["test_durations"][0]["duration_seconds"] == 0.25


def test_resume_refuses_passed_lane_without_counts(tmp_path: Path) -> None:
    journal = tmp_path / "malformed-resume.jsonl"
    identity = RunIdentity.current()
    orchestrator = RegressionOrchestrator(journal_path=journal, resume=True)
    journal.write_text(
        json.dumps(
            {
                "event": "run_initialized",
                "identity": identity.to_dict(),
                "execution_plan_sha256": orchestrator._execution_plan_digest(orchestrator.lanes),
            }
        )
        + "\n"
        + json.dumps({"event": "lane_completed", "lane_id": "unit-01", "passed": True})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing integer test counts"):
        orchestrator._validate_or_init_journal()


def test_resume_refuses_changed_worker_plan(tmp_path: Path) -> None:
    journal = tmp_path / "changed-plan.jsonl"
    lane = Lane(
        "integration-batch-01",
        TestTier.INTEGRATION_PROCESS,
        True,
        ("tests/test_parallel.py::test_one",),
        900.0,
    )
    changed_lane = Lane(
        "integration-batch-01",
        TestTier.INTEGRATION_PROCESS,
        True,
        ("tests/test_parallel.py::test_one", "tests/test_parallel.py::test_two"),
        900.0,
    )
    orchestrator = RegressionOrchestrator(lanes=(changed_lane,), journal_path=journal, resume=True)
    journal.write_text(
        json.dumps(
            {
                "event": "run_initialized",
                "identity": orchestrator.identity.to_dict(),
                "execution_plan_sha256": orchestrator._execution_plan_digest((lane,)),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="execution plan does not match"):
        orchestrator._validate_or_init_journal()


def test_parse_pytest_counts() -> None:
    """Verify regex extraction of passed, failed, skipped, and subtest counts."""
    out = "====== 4247 passed, 3 skipped, 26 subtests passed in 100.0s ======"
    p, f, s, sub = parse_pytest_counts(out)
    assert p == 4247
    assert f == 0
    assert s == 3
    assert sub == 26

    out_fail = "====== 5 failed, 20 passed in 12.0s ======"
    p, f, s, sub = parse_pytest_counts(out_fail)
    assert p == 20
    assert f == 5
    assert s == 0
    assert sub == 0

    durations = parse_pytest_durations(
        "0.03s setup    tests\\test_alpha.py::test_one\n"
        "1.25s call     tests/test_beta.py::test_two[value with space]\n"
        "not a duration line\n"
    )
    assert durations == (
        PytestDuration("tests/test_alpha.py::test_one", "setup", 0.03),
        PytestDuration("tests/test_beta.py::test_two[value with space]", "call", 1.25),
    )


def test_parse_collected_nodeids_ignores_summary_and_normalizes_windows_paths() -> None:
    output = (
        "tests\\test_alpha.py::test_one\n"
        "tests/test_beta.py::test_two[value]\n\n"
        "2 tests collected in 0.01s\n"
    )
    assert parse_collected_nodeids(output) == (
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::test_two[value]",
    )


def test_existing_journal_requires_explicit_resume(tmp_path: Path) -> None:
    journal = tmp_path / "existing.jsonl"
    identity = RunIdentity.current()
    journal.write_text(
        json.dumps({"event": "run_initialized", "identity": identity.to_dict()}) + "\n",
        encoding="utf-8",
    )
    orchestrator = RegressionOrchestrator(journal_path=journal)
    with pytest.raises(ValueError, match="already exists"):
        orchestrator._validate_or_init_journal()


def test_component_planner_fail_closed_boundaries_preserved() -> None:
    """Security, schema, lifecycle, and shared orchestration changes escalate to FULL."""
    # Bounded capability changes use component mode
    plan_cap = derive_ci_validation_plan(("src/stagemesh/capabilities.py",))
    assert plan_cap.mode == COMPONENT

    # Unknown or shared core boundaries fail closed to full mode
    for shared_path in (
        "src/stagemesh/coordinator.py",
        "src/stagemesh/security.py",
        "src/stagemesh/persistence.py",
        ".github/workflows/ci.yml",
        "src/stagemesh/test_taxonomy.py",
    ):
        plan = derive_ci_validation_plan((shared_path,))
        assert plan.mode == FULL, f"Expected {shared_path} to require FULL regression"


def test_catalog_completeness_and_canonical_bijection() -> None:
    """Every test file in the repository must be classified in exactly one tier."""
    assert len(FILE_TIER_MAP) == 211

    # All 5 tiers must have represented tests
    tiers_represented = set(FILE_TIER_MAP.values())
    assert tiers_represented == set(TestTier)

    # Check digest and fingerprint stability
    digest = catalog_digest()
    fingerprint = policy_fingerprint()
    assert isinstance(digest, str) and len(digest) == 64
    assert isinstance(fingerprint, str) and len(fingerprint) == 64

    # Build default lanes and verify no file is lost or duplicated
    default_lanes = build_default_lanes()
    lane_files = [f for lane in default_lanes for f in lane.files]
    assert len(lane_files) == 211
    assert set(lane_files) == set(FILE_TIER_MAP.keys())
    assert len(lane_files) == len(set(lane_files)), "Duplicate files found across lanes!"


def test_catalog_rejects_unreviewed_or_invalid_runtime_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taxonomy = __import__("stagemesh.test_taxonomy", fromlist=["FILE_RUNTIME_SECONDS"])
    nodeid = "tests/test_docs_links.py::test_documentation_links_resolve"

    monkeypatch.setitem(taxonomy.FILE_RUNTIME_SECONDS, "tests/test_not_catalogued.py", 1.0)
    with pytest.raises(ValueError, match="outside the test catalog"):
        validate_catalog((nodeid,), allow_uncollected_catalog_entries=True)
    monkeypatch.delitem(taxonomy.FILE_RUNTIME_SECONDS, "tests/test_not_catalogued.py")

    monkeypatch.setitem(taxonomy.FILE_RUNTIME_SECONDS, "tests/test_docs_links.py", 0.0)
    with pytest.raises(ValueError, match="positive numbers"):
        validate_catalog((nodeid,), allow_uncollected_catalog_entries=True)


def test_live_canonical_collection_equals_tier_union() -> None:
    jobs = 2
    orchestrator = RegressionOrchestrator(jobs=jobs)
    try:
        evidence = orchestrator._audit_collection()
        execution_lanes = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()
    assert evidence["canonical_count"] >= 4_200
    assert evidence["duplicate_count"] == 0
    assert evidence["union_matches_canonical"] is True
    assert sum(evidence["tier_counts"].values()) == evidence["canonical_count"]
    assert evidence["canonical_nodeids"] == list(orchestrator._canonical_nodeids)
    execution_nodeids = tuple(target for lane in execution_lanes for target in lane.files)
    assert len(execution_nodeids) == len(orchestrator._canonical_nodeids)
    assert set(execution_nodeids) == set(orchestrator._canonical_nodeids)
    parallel_tiers = {
        lane.tier for lane in execution_lanes if lane.parallel_safe
    }
    serial_tiers = {
        lane.tier for lane in execution_lanes if not lane.parallel_safe
    }
    assert len(execution_lanes) <= 2 * jobs * len(parallel_tiers) + len(serial_tiers)
    assert len(execution_lanes) < len(build_default_lanes())
    assert all(
        not lane.parallel_safe
        for lane in execution_lanes
        if lane.tier is TestTier.NATIVE_PLATFORM
    )


def test_execution_lane_planner_is_deterministic_balanced_and_file_atomic() -> None:
    files = tuple(f"tests/test_lane_{index}.py" for index in range(6))
    lanes = tuple(
        Lane(
            f"integration-{index}",
            TestTier.INTEGRATION_PROCESS,
            True,
            (file_path,),
            900.0,
        )
        for index, file_path in enumerate(files)
    )
    counts = (8, 7, 6, 5, 4, 3)
    nodeids = tuple(
        f"{file_path}::test_{test_index}"
        for file_path, count in zip(files, counts, strict=True)
        for test_index in range(count)
    )
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=2)
    orchestrator._canonical_nodeids = nodeids
    try:
        first = orchestrator._build_execution_lanes()
        second = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()

    assert first == second
    assert len(first) == 4
    assert max(len(lane.files) for lane in first) - min(len(lane.files) for lane in first) <= 2
    assert {nodeid for lane in first for nodeid in lane.files} == set(nodeids)
    lane_by_nodeid = {
        nodeid: lane.lane_id for lane in first for nodeid in lane.files
    }
    for file_path in files:
        assert len({lane_by_nodeid[nodeid] for nodeid in nodeids if nodeid.startswith(file_path + "::")}) == 1


def test_execution_lane_planner_splits_only_measured_hot_files(monkeypatch: pytest.MonkeyPatch) -> None:
    hot_file = "tests/test_hot.py"
    cold_file = "tests/test_cold.py"
    lanes = (
        Lane("hot", TestTier.INTEGRATION_PROCESS, True, (hot_file,), 900.0),
        Lane("cold", TestTier.INTEGRATION_PROCESS, True, (cold_file,), 900.0),
    )
    monkeypatch.setitem(
        __import__("stagemesh.regression_orchestrator", fromlist=["FILE_RUNTIME_SECONDS"]).FILE_RUNTIME_SECONDS,
        hot_file,
        1_201.0,
    )
    nodeids = tuple(f"{hot_file}::test_{index}" for index in range(12)) + tuple(
        f"{cold_file}::test_{index}" for index in range(4)
    )
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=4)
    orchestrator._canonical_nodeids = nodeids
    try:
        execution = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()

    hot_lane_ids = {
        lane.lane_id
        for lane in execution
        if any(nodeid.startswith(hot_file + "::") for nodeid in lane.files)
    }
    cold_lane_ids = {
        lane.lane_id
        for lane in execution
        if any(nodeid.startswith(cold_file + "::") for nodeid in lane.files)
    }
    assert len(hot_lane_ids) == 3
    assert len(cold_lane_ids) == 1


def test_execution_lane_planner_keeps_native_tests_in_one_serial_lane() -> None:
    native_file = "tests/test_native.py"
    lane = Lane(
        "native",
        TestTier.NATIVE_PLATFORM,
        False,
        (native_file,),
        600.0,
    )
    orchestrator = RegressionOrchestrator(lanes=(lane,), jobs=8)
    orchestrator._canonical_nodeids = tuple(
        f"{native_file}::test_{index}" for index in range(25)
    )
    try:
        execution = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()

    assert len(execution) == 1
    assert execution[0].lane_id == "native-batch-01"
    assert execution[0].parallel_safe is False
    assert execution[0].files == orchestrator._canonical_nodeids


def test_collection_audit_rejects_duplicate_tier_union(monkeypatch: pytest.MonkeyPatch) -> None:
    unit_file = "tests/test_attribution_guards.py"
    integration_file = "tests/test_parallel.py"
    nodeid = f"{unit_file}::test_one"
    lanes = (
        Lane("unit", TestTier.DETERMINISTIC_UNIT, True, (unit_file,), 60.0),
        Lane("integration", TestTier.INTEGRATION_PROCESS, True, (integration_file,), 60.0),
    )
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=2)

    def collect(files: tuple[str, ...], _worker_id: int) -> tuple[str, ...]:
        if len(files) == 2:
            return (nodeid, f"{integration_file}::test_two")
        return (nodeid,)

    monkeypatch.setattr(orchestrator, "_collect_nodeids", collect)
    monkeypatch.setattr(
        "stagemesh.regression_orchestrator.validate_catalog",
        lambda nodeids, **_kwargs: {
            item: TestTier.DETERMINISTIC_UNIT if item == nodeid else TestTier.INTEGRATION_PROCESS
            for item in nodeids
        },
    )
    try:
        with pytest.raises(ValueError, match="duplicate nodeids"):
            orchestrator._audit_collection()
    finally:
        orchestrator._pool_isolation.cleanup()


def test_collection_audit_rejects_missing_and_extra_nodeids(monkeypatch: pytest.MonkeyPatch) -> None:
    unit_file = "tests/test_attribution_guards.py"
    integration_file = "tests/test_parallel.py"
    unit_nodeid = f"{unit_file}::test_one"
    integration_nodeid = f"{integration_file}::test_two"
    extra_nodeid = f"{integration_file}::test_extra"
    lanes = (
        Lane("unit", TestTier.DETERMINISTIC_UNIT, True, (unit_file,), 60.0),
        Lane("integration", TestTier.INTEGRATION_PROCESS, True, (integration_file,), 60.0),
    )
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=2)

    def collect(files: tuple[str, ...], _worker_id: int) -> tuple[str, ...]:
        if len(files) == 2:
            return (unit_nodeid, integration_nodeid)
        return (unit_nodeid,) if files == (unit_file,) else (extra_nodeid,)

    monkeypatch.setattr(orchestrator, "_collect_nodeids", collect)
    monkeypatch.setattr(
        "stagemesh.regression_orchestrator.validate_catalog",
        lambda _nodeids, **_kwargs: {
            unit_nodeid: TestTier.DETERMINISTIC_UNIT,
            integration_nodeid: TestTier.INTEGRATION_PROCESS,
        },
    )
    try:
        with pytest.raises(ValueError, match="differs from canonical collection"):
            orchestrator._audit_collection()
    finally:
        orchestrator._pool_isolation.cleanup()


def test_run_fails_when_result_accounting_does_not_match_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lane = Lane("unit", TestTier.DETERMINISTIC_UNIT, True, ("tests/test_docs_links.py",), 60.0)
    report = tmp_path / "report.json"
    orchestrator = RegressionOrchestrator(lanes=(lane,), jobs=1, report_path=report)
    monkeypatch.setattr(
        orchestrator,
        "_audit_collection",
        lambda: {
            "canonical_count": 1,
            "canonical_sha256": "abc",
            "file_counts": {"tests/test_docs_links.py": 1},
        },
    )
    monkeypatch.setattr(orchestrator, "_validate_or_init_journal", lambda *_args: set())
    monkeypatch.setattr(
        orchestrator,
        "_execute_lane",
        lambda _lane, _worker_id: LaneResult(
            lane_id="unit",
            tier=str(TestTier.DETERMINISTIC_UNIT),
            passed=True,
            returncode=0,
            duration_seconds=0.01,
            stdout="1 passed",
            stderr="",
            worker_id=0,
            passed_count=0,
            test_durations=(
                PytestDuration("tests/test_docs_links.py::test_docs_links", "call", 0.75),
            ),
        ),
    )

    assert orchestrator.run() == 1
    report_data = json.loads(report.read_text(encoding="utf-8"))
    assert report_data["verdict"] == "FAILED"
    assert report_data["coverage"] == {
        "accounted_nodeids": 0,
        "expected_nodeids": 1,
        "matches": False,
    }
    assert report_data["slowest_tests"] == [
        {
            "nodeid": "tests/test_docs_links.py::test_docs_links",
            "duration_seconds": 0.75,
        }
    ]


def test_keyboard_interrupt_cancels_run_and_writes_partial_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lane = Lane("unit", TestTier.DETERMINISTIC_UNIT, True, ("tests/test_docs_links.py",), 60.0)
    report = tmp_path / "interrupted.json"
    orchestrator = RegressionOrchestrator(lanes=(lane,), jobs=1, report_path=report)
    monkeypatch.setattr(
        orchestrator,
        "_audit_collection",
        lambda: {
            "canonical_count": 1,
            "canonical_sha256": "abc",
            "file_counts": {"tests/test_docs_links.py": 1},
        },
    )
    monkeypatch.setattr(orchestrator, "_validate_or_init_journal", lambda *_args: set())

    def interrupt(_lane: Lane, _worker_id: int) -> LaneResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "_execute_lane", interrupt)
    assert orchestrator.run() == 130
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "INTERRUPTED"


def test_lane_result_report_omits_pass_output_and_bounds_failure_output() -> None:
    passed = LaneResult(
        "a",
        "unit",
        True,
        0,
        1.0,
        "large output",
        "warning",
        0,
        test_durations=(PytestDuration("tests/test_a.py::test_one", "call", 0.5),),
    )
    failed = LaneResult("b", "unit", False, 1, 1.0, "x" * 25_000, "y" * 25_000, 0)
    assert passed.to_dict()["stdout"] == ""
    assert passed.to_dict()["stderr"] == ""
    assert passed.to_dict()["test_durations"][0]["duration_seconds"] == 0.5
    assert len(failed.to_dict()["stdout"]) == 20_000
    assert len(failed.to_dict()["stderr"]) == 20_000

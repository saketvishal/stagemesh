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
import os
import platform
import signal
import site
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from stagemesh.ci_validation_plan import COMPONENT, FULL, derive_ci_validation_plan
from stagemesh.regression_orchestrator import (
    BATCHES_PER_WORKER_AND_TIER,
    INTEGRATION_BATCHES_PER_WORKER,
    MAX_LOCAL_WORKERS,
    WINDOWS_ESSENTIAL_TIERS,
    Lane,
    LaneResult,
    PytestDuration,
    RegressionOrchestrator,
    RunIdentity,
    _bounded_failure_output,
    _popen_session_kwargs,
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
    TEST_NODEID_OVERRIDES,
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


def test_lane_subprocesses_use_interrupt_isolated_process_groups() -> None:
    kwargs = _popen_session_kwargs()
    if platform.system() == "Windows":
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kwargs == {"start_new_session": True}


def test_failure_output_keeps_traceback_prefix_and_summary_suffix() -> None:
    output = "TRACEBACK\n" + ("x" * 30_000) + "\nFAILED SUMMARY"
    bounded = _bounded_failure_output(output, limit=2_000)
    assert len(bounded) == 2_000
    assert bounded.startswith("TRACEBACK\n")
    assert bounded.endswith("\nFAILED SUMMARY")
    assert "characters omitted" in bounded


def test_real_parent_interrupt_writes_partial_report_and_stops_lane(tmp_path: Path) -> None:
    slow_test = tmp_path / "test_slow.py"
    marker = tmp_path / "lane-started"
    slow_test.write_text(
        "import time\nfrom pathlib import Path\n"
        f"def test_slow():\n    Path({str(marker)!r}).write_text('started')\n    time.sleep(60)\n",
        encoding="utf-8",
    )
    journal = tmp_path / "journal.jsonl"
    report = tmp_path / "report.json"
    code = (
        "from pathlib import Path\n"
        "from stagemesh.regression_orchestrator import Lane, RegressionOrchestrator\n"
        "from stagemesh.test_taxonomy import TestTier\n"
        f"root=Path({str(tmp_path)!r})\n"
        f"target={str(slow_test)!r}+'::test_slow'\n"
        f"lane=Lane('slow',TestTier.INTEGRATION_PROCESS,True,(target,),60.0)\n"
        f"o=RegressionOrchestrator(lanes=(lane,),jobs=1,repo_root=root,journal_path=Path({str(journal)!r}),report_path=Path({str(report)!r}))\n"
        "o._audit_collection=lambda:{'canonical_count':1,'canonical_sha256':'abc','file_counts':{target.split('::',1)[0]:1}}\n"
        "o._build_execution_lanes=lambda:(lane,)\n"
        "raise SystemExit(o.run())\n"
    )
    env = dict(os.environ)
    import_roots = [
        str(Path(__file__).resolve().parents[1] / "src"),
        str(Path(pytest.__file__).resolve().parent.parent),
    ]
    if env.get("PYTHONPATH"):
        import_roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(import_roots)
    kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if platform.system() == "Windows"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, **kwargs)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "orchestrator did not start its lane"
        proc.send_signal(
            signal.CTRL_BREAK_EVENT if platform.system() == "Windows" else signal.SIGINT
        )
        assert proc.wait(timeout=30) == 130
        assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "INTERRUPTED"
    finally:
        if proc.poll() is None:
            kill_process_tree(proc.pid)


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
        assert contract.concurrency_group
        assert contract.max_parallel_workers > 0
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
    assert TIER_ISOLATION_CONTRACTS[TestTier.DETERMINISTIC_UNIT].max_parallel_workers == 16
    assert TIER_ISOLATION_CONTRACTS[TestTier.COMPONENT_CONTRACT].max_parallel_workers == 16
    assert TIER_ISOLATION_CONTRACTS[TestTier.INTEGRATION_PROCESS].max_parallel_workers == 8
    assert TIER_ISOLATION_CONTRACTS[TestTier.SOAK_FAILURE_INJECTION].max_parallel_workers == 8
    assert TIER_ISOLATION_CONTRACTS[TestTier.NATIVE_PLATFORM].max_parallel_workers == 1
    assert TIER_ISOLATION_CONTRACTS[TestTier.SERIAL_TIMING].parallel_safe is False
    assert TIER_ISOLATION_CONTRACTS[TestTier.SERIAL_TIMING].max_parallel_workers == 1
    assert MAX_LOCAL_WORKERS == 8
    assert BATCHES_PER_WORKER_AND_TIER == 4
    assert INTEGRATION_BATCHES_PER_WORKER == 8
    assert (
        TIER_ISOLATION_CONTRACTS[TestTier.INTEGRATION_PROCESS].concurrency_group
        == TIER_ISOLATION_CONTRACTS[TestTier.SOAK_FAILURE_INJECTION].concurrency_group
    )
    assert tier_for_test("tests/test_continuous_driver.py::test_process_recovery") is TestTier.NATIVE_PLATFORM
    assert tier_for_test("tests/test_regression_architecture_acceptance.py") is TestTier.NATIVE_PLATFORM
    assert tier_for_test("tests/test_provider_outage_recovery.py") is TestTier.SERIAL_TIMING
    assert (
        tier_for_test("tests/test_workspace_ownership.py::test_dead_owner_then_outside_commit_is_not_recovered")
        is TestTier.NATIVE_PLATFORM
    )
    assert tier_for_test("tests/test_workspace_ownership.py::test_authorized_agent_edit_is_committed_and_sealed_as_the_candidate") is TestTier.INTEGRATION_PROCESS
    timing_lanes = [lane for lane in build_default_lanes() if lane.tier is TestTier.SERIAL_TIMING]
    assert len(timing_lanes) == 1
    assert timing_lanes[0].parallel_safe is False
    assert timing_lanes[0].files == ("tests/test_provider_outage_recovery.py",)
    native_lanes = [lane for lane in build_default_lanes() if lane.tier is TestTier.NATIVE_PLATFORM]
    assert len(native_lanes) == 1
    assert "tests/test_workspace_ownership.py" in native_lanes[0].files


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
        assert ctx0.env["PYTHONUSERBASE"] == str(site.getuserbase())

        nested_env = dict(ctx0.env)
        nested_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        nested = subprocess.run(
            [sys.executable, "-c", "import pytest"],
            env=nested_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert nested.returncode == 0, nested.stderr
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

    journal = tmp_path / "timeout.jsonl"
    orchestrator = RegressionOrchestrator(
        lanes=(lane,),
        jobs=1,
        repo_root=tmp_path,
        journal_path=journal,
    )
    res = orchestrator._execute_lane(lane, worker_id=0)

    assert res.passed is False
    assert res.timed_out is True
    assert res.returncode == 124
    assert "timeout" in res.error_message.lower()
    record = json.loads(journal.read_text(encoding="utf-8"))
    assert record["timed_out"] is True
    assert "timeout" in record["error_message"].lower()
    assert set(record) >= {"stdout", "stderr"}


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
    assert len(FILE_TIER_MAP) == 215

    # All tiers must have represented tests
    tiers_represented = set(FILE_TIER_MAP.values())
    assert tiers_represented == set(TestTier)

    # Check digest and fingerprint stability
    digest = catalog_digest()
    fingerprint = policy_fingerprint()
    assert isinstance(digest, str) and len(digest) == 64
    assert isinstance(fingerprint, str) and len(fingerprint) == 64

    # Build default lanes and verify no catalog file is lost. A file with an
    # explicit node-level override is intentionally present in both tier
    # manifests; execution planning partitions its node IDs exactly once.
    default_lanes = build_default_lanes()
    lane_files = [f for lane in default_lanes for f in lane.files]
    assert set(lane_files) == set(FILE_TIER_MAP.keys())
    duplicated_files = {
        file_path for file_path, count in Counter(lane_files).items() if count > 1
    }
    assert duplicated_files == {"tests/test_workspace_ownership.py"}


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
    lane_tier_by_nodeid = {
        nodeid: lane.tier for lane in execution_lanes for nodeid in lane.files
    }
    workspace_nodeids = {
        nodeid
        for nodeid in orchestrator._canonical_nodeids
        if nodeid.startswith("tests/test_workspace_ownership.py::")
    }
    assert workspace_nodeids & set(TEST_NODEID_OVERRIDES) == set(TEST_NODEID_OVERRIDES)
    assert {
        nodeid
        for nodeid in workspace_nodeids
        if lane_tier_by_nodeid[nodeid] is TestTier.NATIVE_PLATFORM
    } == set(TEST_NODEID_OVERRIDES)
    assert all(
        lane_tier_by_nodeid[nodeid] is TestTier.INTEGRATION_PROCESS
        for nodeid in workspace_nodeids - set(TEST_NODEID_OVERRIDES)
    )
    parallel_tiers = {
        lane.tier for lane in execution_lanes if lane.parallel_safe
    }
    serial_tiers = {
        lane.tier for lane in execution_lanes if not lane.parallel_safe
    }
    assert len(execution_lanes) <= (
        sum(
            jobs
            * (
                INTEGRATION_BATCHES_PER_WORKER
                if tier is TestTier.INTEGRATION_PROCESS
                else BATCHES_PER_WORKER_AND_TIER
            )
            for tier in parallel_tiers
        )
        + len(serial_tiers)
    )
    assert len(execution_lanes) < len(build_default_lanes())
    assert all(
        not lane.parallel_safe
        for lane in execution_lanes
        if lane.tier is TestTier.NATIVE_PLATFORM
    )


def test_windows_essential_profile_is_exact_and_never_claims_complete_catalog() -> None:
    orchestrator = RegressionOrchestrator(
        jobs=1,
        selected_tiers=WINDOWS_ESSENTIAL_TIERS,
    )
    try:
        evidence = orchestrator._audit_collection()
        execution_lanes = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()

    assert evidence["scope"] == "tier_subset"
    assert evidence["complete_catalog"] is False
    assert evidence["selected_tiers"] == ["native_platform", "serial_timing"]
    assert {lane.tier for lane in execution_lanes} == WINDOWS_ESSENTIAL_TIERS
    assert all(not lane.parallel_safe for lane in execution_lanes)
    execution_nodeids = [nodeid for lane in execution_lanes for nodeid in lane.files]
    execution_counts = Counter(
        lane.tier.value for lane in execution_lanes for _nodeid in lane.files
    )
    assert dict(execution_counts) == evidence["tier_counts"]
    assert len(execution_nodeids) == len(set(execution_nodeids)) == evidence["canonical_count"]


def test_execution_lane_planner_is_deterministic_balanced_and_file_atomic() -> None:
    files = tuple(f"tests/test_lane_{index}.py" for index in range(12))
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
    counts = tuple(range(12, 0, -1))
    nodeids = tuple(
        f"{file_path}::test_{test_index}"
        for file_path, count in zip(files, counts, strict=True)
        for test_index in range(count)
    )
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=1)
    orchestrator._canonical_nodeids = nodeids
    try:
        first = orchestrator._build_execution_lanes()
        second = orchestrator._build_execution_lanes()
    finally:
        orchestrator._pool_isolation.cleanup()

    assert first == second
    assert len(first) == INTEGRATION_BATCHES_PER_WORKER
    assert sorted(len(lane.files) for lane in first) == [9, 9, 9, 9, 9, 10, 11, 12]
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

    def collect(_files: tuple[str, ...], _worker_id: int) -> tuple[str, ...]:
        return (nodeid, nodeid)

    monkeypatch.setattr(orchestrator, "_collect_nodeids", collect)
    monkeypatch.setattr(
        "stagemesh.regression_orchestrator.validate_catalog",
        lambda nodeids, **_kwargs: {
            item: TestTier.DETERMINISTIC_UNIT if item == nodeid else TestTier.INTEGRATION_PROCESS
            for item in nodeids
        },
    )
    try:
        with pytest.raises(ValueError, match="canonical collection contains duplicate nodeids"):
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

    def collect(_files: tuple[str, ...], _worker_id: int) -> tuple[str, ...]:
        return (unit_nodeid, integration_nodeid)

    monkeypatch.setattr(orchestrator, "_collect_nodeids", collect)
    monkeypatch.setattr(
        "stagemesh.regression_orchestrator.validate_catalog",
        lambda _nodeids, **_kwargs: {
            unit_nodeid: TestTier.DETERMINISTIC_UNIT,
            extra_nodeid: TestTier.INTEGRATION_PROCESS,
        },
    )
    try:
        with pytest.raises(ValueError, match="tier classification differs from canonical collection"):
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


def test_subtests_are_reported_without_double_counting_collected_nodeids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lane = Lane("unit", TestTier.DETERMINISTIC_UNIT, True, ("tests/test_docs_links.py",), 60.0)
    report = tmp_path / "subtests.json"
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
            stdout="1 passed, 1 subtests passed",
            stderr="",
            worker_id=0,
            passed_count=1,
            subtests_passed=1,
        ),
    )

    assert orchestrator.run() == 0
    report_data = json.loads(report.read_text(encoding="utf-8"))
    assert report_data["aggregate_counts"]["subtests_passed"] == 1
    assert report_data["coverage"] == {
        "accounted_nodeids": 1,
        "expected_nodeids": 1,
        "matches": True,
    }


def test_external_process_group_never_exceeds_eight_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lanes = tuple(
        Lane(
            f"integration-{index}",
            TestTier.INTEGRATION_PROCESS,
            True,
            (f"tests/synthetic_{index}.py",),
            60.0,
        )
        for index in range(12)
    )
    report = tmp_path / "group-cap.json"
    orchestrator = RegressionOrchestrator(lanes=lanes, jobs=16, report_path=report)
    monkeypatch.setattr(
        orchestrator,
        "_audit_collection",
        lambda: {
            "canonical_count": len(lanes),
            "canonical_sha256": "abc",
            "file_counts": {lane.files[0]: 1 for lane in lanes},
        },
    )
    monkeypatch.setattr(orchestrator, "_validate_or_init_journal", lambda *_args: set())
    lock = threading.Lock()
    active = 0
    maximum = 0

    def execute(lane: Lane, worker_id: int) -> LaneResult:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return LaneResult(
            lane.lane_id,
            str(lane.tier),
            True,
            0,
            0.05,
            "1 passed",
            "",
            worker_id,
            passed_count=1,
        )

    monkeypatch.setattr(orchestrator, "_execute_lane", execute)
    assert orchestrator.run() == 0
    assert maximum == 8
    plan = json.loads(report.read_text(encoding="utf-8"))["execution_plan"]
    assert {item["concurrency_group"] for item in plan} == {"external_process"}
    assert {item["max_parallel_workers"] for item in plan} == {8}


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

"""Provider-neutral, hermetic regression orchestrator for StageMesh.

Executes test lanes with machine-enforced isolation contracts, reliable process tree
cleanup, live progress streaming, append-only identity-bound journals, and safe resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path

from .test_isolation import RegressionRunLock, WorkerPoolIsolation, kill_process_tree
from .test_taxonomy import (
    FILE_RUNTIME_SECONDS,
    FILE_TIER_MAP,
    TEST_NODEID_OVERRIDES,
    TIER_ISOLATION_CONTRACTS,
    TestTier,
    catalog_digest,
    normalize_test_path,
    policy_fingerprint,
    validate_catalog,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_SLOWEST_TEST_LIMIT = 50
MAX_LOCAL_WORKERS = 8
TARGET_BATCH_SECONDS = 600.0
BATCHES_PER_WORKER_AND_TIER = 4
INTEGRATION_BATCHES_PER_WORKER = 6


def _bounded_failure_output(value: str, limit: int = 20_000) -> str:
    """Retain both the failure traceback and summary within a journal bound."""
    if len(value) <= limit:
        return value
    marker = f"\n... [{len(value) - limit} characters omitted] ...\n"
    available = limit - len(marker)
    prefix = available // 2
    suffix = available - prefix
    return value[:prefix] + marker + value[-suffix:]


def _popen_session_kwargs() -> dict[str, object]:
    """Separate lane process groups so parent interruption can preserve evidence."""
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@dataclass(frozen=True)
class Lane:
    """An execution lane grouping pytest targets with identical isolation contracts."""

    lane_id: str
    tier: TestTier
    parallel_safe: bool
    files: tuple[str, ...]
    timeout_seconds: float
    estimated_seconds: float = 0.0

    def to_dict(self) -> dict:
        contract = TIER_ISOLATION_CONTRACTS[self.tier]
        return {
            "lane_id": self.lane_id,
            "tier": str(self.tier),
            "parallel_safe": self.parallel_safe,
            "files": list(self.files),
            "timeout_seconds": self.timeout_seconds,
            "estimated_seconds": round(self.estimated_seconds, 3),
            "concurrency_group": contract.concurrency_group,
            "max_parallel_workers": contract.max_parallel_workers,
        }


@dataclass
class PytestDuration:
    """One pytest setup/call/teardown timing record."""

    nodeid: str
    phase: str
    duration_seconds: float


@dataclass
class LaneResult:
    """Execution outcome of a single test lane."""

    lane_id: str
    tier: str
    passed: bool
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str
    worker_id: int
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    subtests_passed: int = 0
    timed_out: bool = False
    error_message: str = ""
    test_durations: tuple[PytestDuration, ...] = ()

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.passed:
            result["stdout"] = ""
            result["stderr"] = ""
        else:
            result["stdout"] = self.stdout[-20_000:]
            result["stderr"] = self.stderr[-20_000:]
        return result


@dataclass(frozen=True)
class RunIdentity:
    """Cryptographically verifiable identity of a regression run."""

    commit_sha: str
    catalog_digest: str
    policy_fingerprint: str
    interpreter: str
    python_version: str
    operating_system: str
    os_release: str
    dependencies_hash: str
    expected_ci_head_sha: str = ""
    tree_state: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def current(cls, repo_root: Path = ROOT) -> RunIdentity:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            commit_sha = res.stdout.strip() if res.returncode == 0 else "unknown_head"
        except (OSError, subprocess.SubprocessError):
            commit_sha = "unknown_head"

        tree_hasher = hashlib.sha256()
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD", "--"],
                cwd=repo_root,
                capture_output=True,
                check=False,
            )
            status_text = status.stdout if status.returncode == 0 else "status_unavailable"
            tree_hasher.update(status_text.encode("utf-8", errors="replace"))
            tree_hasher.update(diff.stdout if diff.returncode == 0 else b"diff_unavailable")
            for line in status_text.splitlines():
                if not line.startswith("?? "):
                    continue
                path = repo_root / line[3:]
                if path.is_file():
                    tree_hasher.update(line[3:].encode("utf-8", errors="replace"))
                    tree_hasher.update(path.read_bytes())
            tree_state = "clean" if not status_text else f"dirty:{tree_hasher.hexdigest()}"
        except (OSError, subprocess.SubprocessError, UnicodeError):
            tree_state = "unknown"

        # Compute dependencies signature from pyproject.toml
        dep_hasher = hashlib.sha256()
        for dependency_file in ("pyproject.toml", "build_backend.py"):
            path = repo_root / dependency_file
            if path.exists():
                dep_hasher.update(dependency_file.encode("utf-8"))
                dep_hasher.update(path.read_bytes())
        deps_hash = dep_hasher.hexdigest()

        return cls(
            commit_sha=commit_sha,
            catalog_digest=catalog_digest(),
            policy_fingerprint=policy_fingerprint(),
            interpreter=sys.executable,
            python_version=sys.version.split()[0],
            operating_system=platform.system(),
            os_release=platform.release(),
            dependencies_hash=deps_hash,
            expected_ci_head_sha=os.environ.get("STAGEMESH_CI_HEAD_SHA", ""),
            tree_state=tree_state,
        )


def build_default_lanes() -> tuple[Lane, ...]:
    """Build file-granular parallel lanes and explicit bounded serial lanes."""
    tier_files: dict[TestTier, list[str]] = {tier: [] for tier in TestTier}
    for file_path, tier in FILE_TIER_MAP.items():
        tier_files[tier].append(file_path)
    for nodeid, tier in TEST_NODEID_OVERRIDES.items():
        file_path = nodeid.split("::", 1)[0]
        if file_path not in tier_files[tier]:
            tier_files[tier].append(file_path)

    lanes: list[Lane] = []

    prefixes = {
        TestTier.DETERMINISTIC_UNIT: "unit",
        TestTier.COMPONENT_CONTRACT: "component",
        TestTier.INTEGRATION_PROCESS: "integration",
        TestTier.SOAK_FAILURE_INJECTION: "soak",
    }
    for tier, prefix in prefixes.items():
        contract = TIER_ISOLATION_CONTRACTS[tier]
        for index, file_path in enumerate(sorted(tier_files[tier]), start=1):
            lanes.append(
                Lane(
                    lane_id=f"{prefix}-{index:03d}",
                    tier=tier,
                    parallel_safe=contract.parallel_safe,
                    files=(file_path,),
                    timeout_seconds=contract.timeout_seconds,
                )
            )

    # Native platform files are deliberately serialized in one bounded lane.
    native_files = sorted(tier_files[TestTier.NATIVE_PLATFORM])
    if native_files:
        lanes.append(
            Lane(
                lane_id="native-platform-serial",
                tier=TestTier.NATIVE_PLATFORM,
                parallel_safe=False,
                files=tuple(native_files),
                timeout_seconds=TIER_ISOLATION_CONTRACTS[TestTier.NATIVE_PLATFORM].timeout_seconds * len(native_files),
            )
        )

    timing_files = sorted(tier_files[TestTier.SERIAL_TIMING])
    if timing_files:
        contract = TIER_ISOLATION_CONTRACTS[TestTier.SERIAL_TIMING]
        lanes.append(
            Lane(
                lane_id="serial-timing",
                tier=TestTier.SERIAL_TIMING,
                parallel_safe=False,
                files=tuple(timing_files),
                timeout_seconds=contract.timeout_seconds,
            )
        )

    return tuple(lanes)


_PASSED_PATTERN = re.compile(r"(\d+)\s+passed")
_FAILED_PATTERN = re.compile(r"(\d+)\s+failed")
_SKIPPED_PATTERN = re.compile(r"(\d+)\s+skipped")
_SUBTESTS_PATTERN = re.compile(r"(\d+)\s+subtests passed")
_DURATION_PATTERN = re.compile(
    r"^\s*(?P<seconds>\d+(?:\.\d+)?)s\s+"
    r"(?P<phase>setup|call|teardown)\s+"
    r"(?P<nodeid>tests[/\\].+?)\s*$"
)


def parse_pytest_counts(output: str) -> tuple[int, int, int, int]:
    """Extract passed, failed, skipped, and subtest counts from pytest terminal output."""
    passed = 0
    failed = 0
    skipped = 0
    subtests = 0

    passed_match = _PASSED_PATTERN.search(output)
    if passed_match:
        passed = int(passed_match.group(1))

    failed_match = _FAILED_PATTERN.search(output)
    if failed_match:
        failed = int(failed_match.group(1))

    skipped_match = _SKIPPED_PATTERN.search(output)
    if skipped_match:
        skipped = int(skipped_match.group(1))

    subtests_match = _SUBTESTS_PATTERN.search(output)
    if subtests_match:
        subtests = int(subtests_match.group(1))

    return passed, failed, skipped, subtests


def parse_pytest_durations(output: str) -> tuple[PytestDuration, ...]:
    """Extract bounded per-test phase timings from pytest ``--durations=0`` output."""
    durations: list[PytestDuration] = []
    for line in output.splitlines():
        match = _DURATION_PATTERN.match(line)
        if not match:
            continue
        durations.append(
            PytestDuration(
                nodeid=normalize_test_path(match.group("nodeid")),
                phase=match.group("phase"),
                duration_seconds=float(match.group("seconds")),
            )
        )
    return tuple(durations)


def parse_collected_nodeids(output: str) -> tuple[str, ...]:
    """Parse pytest ``--collect-only -q`` output without trusting its summary count."""
    nodeids: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith(("tests/", "tests\\")) and "::" in line:
            nodeids.append(normalize_test_path(line))
    return tuple(nodeids)


class RegressionOrchestrator:
    """Manages full or partial regression execution across isolated lanes."""

    def __init__(
        self,
        lanes: Sequence[Lane] | None = None,
        jobs: int | None = None,
        repo_root: Path = ROOT,
        journal_path: Path | None = None,
        resume: bool = False,
        report_path: Path | None = None,
        selected_tier: str | None = None,
    ) -> None:
        self.repo_root = repo_root
        all_lanes = lanes if lanes is not None else build_default_lanes()
        self.selected_tier = selected_tier
        if selected_tier:
            all_lanes = tuple(l for l in all_lanes if str(l.tier) == selected_tier or l.tier.name.lower() == selected_tier.lower())
        self.lanes = tuple(all_lanes)

        max_jobs = min(MAX_LOCAL_WORKERS, max(1, os.cpu_count() or 2))
        self.jobs = max(1, jobs if jobs is not None else max_jobs)
        self.journal_path = journal_path
        self.resume = resume
        self.report_path = report_path
        self.identity = RunIdentity.current(repo_root=self.repo_root)

        self._journal_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._active_processes: dict[int, subprocess.Popen[str]] = {}
        self._resumed_records: dict[str, dict[str, object]] = {}
        self._pool_isolation = WorkerPoolIsolation()
        self._run_lock = RegressionRunLock(repo_root)
        self.collection_evidence: dict[str, object] | None = None
        self._canonical_nodeids: tuple[str, ...] = ()
        self._classified_nodeids: dict[str, TestTier] = {}

    def _register_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes[process.pid] = process

    def _unregister_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes.pop(process.pid, None)

    def _terminate_active_processes(self) -> None:
        with self._process_lock:
            processes = tuple(self._active_processes.values())
        for process in processes:
            if process.poll() is None:
                kill_process_tree(process.pid)

    def _collect_nodeids(self, files: Sequence[str], worker_id: int) -> tuple[str, ...]:
        """Collect exactly ``files`` in a bounded isolated subprocess."""
        context = self._pool_isolation.get_worker_context(worker_id)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            *files,
        ]
        process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            env=context.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_popen_session_kwargs(),
        )
        self._register_process(process)
        try:
            stdout, stderr = process.communicate(timeout=300)
        except subprocess.TimeoutExpired as exc:
            kill_process_tree(process.pid)
            process.communicate()
            raise ValueError(f"pytest collection timed out for {len(files)} paths") from exc
        finally:
            self._unregister_process(process)
        if process.returncode != 0:
            detail = (stdout + "\n" + stderr)[-4000:]
            raise ValueError(f"pytest collection failed for {len(files)} paths:\n{detail}")
        nodeids = parse_collected_nodeids(stdout)
        if not nodeids:
            raise ValueError(f"pytest collection returned no nodeids for {len(files)} paths")
        return nodeids

    def _audit_collection(self) -> dict[str, object]:
        """Prove canonical collection has one complete, duplicate-free tier partition."""
        all_files = tuple(sorted({file_path for lane in self.lanes for file_path in lane.files}))
        if not all_files:
            raise ValueError("no regression files selected")
        worker_base = self.jobs + 10
        collected = self._collect_nodeids(all_files, worker_base)
        duplicate_nodeids = sorted(
            nodeid for nodeid, count in Counter(collected).items() if count != 1
        )
        if duplicate_nodeids:
            raise ValueError(f"canonical collection contains duplicate nodeids: {duplicate_nodeids[:5]}")
        classified = validate_catalog(
            collected,
            allow_uncollected_catalog_entries=self.selected_tier is not None,
        )
        if set(classified) != set(collected) or len(classified) != len(collected):
            missing = sorted(set(collected) - set(classified))
            extra = sorted(set(classified) - set(collected))
            raise ValueError(
                "tier classification differs from canonical collection: "
                f"missing={missing[:5]} extra={extra[:5]}"
            )
        selected_tiers = {lane.tier for lane in self.lanes}
        canonical = tuple(
            nodeid
            for nodeid in collected
            if self.selected_tier is None or classified[nodeid] in selected_tiers
        )
        classified = {nodeid: classified[nodeid] for nodeid in canonical}
        self._canonical_nodeids = canonical
        self._classified_nodeids = classified

        tier_collections = {
            tier: tuple(nodeid for nodeid in canonical if classified[nodeid] is tier)
            for tier in sorted(set(classified.values()), key=lambda item: item.value)
        }
        combined = tuple(
            nodeid
            for tier in sorted(tier_collections, key=lambda item: item.value)
            for nodeid in tier_collections[tier]
        )
        if Counter(combined) != Counter(canonical):
            missing = sorted((Counter(canonical) - Counter(combined)).elements())
            extra = sorted((Counter(combined) - Counter(canonical)).elements())
            raise ValueError(
                "tier partition differs from canonical collection: "
                f"missing={missing[:5]} extra={extra[:5]}"
            )

        canonical_payload = "\n".join(canonical).encode("utf-8")
        file_counts = Counter(nodeid.split("::", 1)[0] for nodeid in canonical)
        evidence: dict[str, object] = {
            "canonical_count": len(canonical),
            "canonical_sha256": hashlib.sha256(canonical_payload).hexdigest(),
            "canonical_nodeids": list(canonical),
            "tier_counts": {
                tier.value: len(tier_collections[tier])
                for tier in sorted(tier_collections, key=lambda item: item.value)
            },
            "duplicate_count": 0,
            "union_matches_canonical": True,
            "file_counts": dict(sorted(file_counts.items())),
        }
        self.collection_evidence = evidence
        return evidence

    def _build_execution_lanes(self) -> tuple[Lane, ...]:
        """Pack audited files into deterministic, worker-balanced execution lanes.

        A pytest process is intentionally the unit of isolation.  The previous
        planner made one process for every twelve node IDs, which turned the
        current catalog into hundreds of short-lived Python processes and made
        Windows process startup part of nearly every test.  This planner keeps
        every file intact, uses the live canonical collection as its weight,
        and creates at most two parallel lanes per worker and tier. Files whose
        measured Windows runtime exceeds the batch target are split into a few
        stable node-ID ranges; the former blanket twelve-test sharding is not
        used. Native platform tests retain one explicit serial lane.
        """
        if not self._canonical_nodeids:
            return self.lanes
        by_file: dict[str, list[str]] = {}
        for nodeid in self._canonical_nodeids:
            by_file.setdefault(nodeid.split("::", 1)[0], []).append(nodeid)

        execution: list[Lane] = []
        manifests_by_tier: dict[TestTier, list[Lane]] = {}
        for manifest_lane in self.lanes:
            manifests_by_tier.setdefault(manifest_lane.tier, []).append(manifest_lane)

        prefixes = {
            TestTier.DETERMINISTIC_UNIT: "unit",
            TestTier.COMPONENT_CONTRACT: "component",
            TestTier.INTEGRATION_PROCESS: "integration",
            TestTier.SOAK_FAILURE_INJECTION: "soak",
            TestTier.NATIVE_PLATFORM: "native",
            TestTier.SERIAL_TIMING: "timing",
        }
        canonical_order = {nodeid: index for index, nodeid in enumerate(self._canonical_nodeids)}
        for tier in sorted(manifests_by_tier, key=lambda item: item.value):
            manifests = manifests_by_tier[tier]
            contract = TIER_ISOLATION_CONTRACTS[tier]
            planning_units: list[tuple[str, int, tuple[str, ...], float]] = []
            for manifest in manifests:
                for file_path in manifest.files:
                    file_targets = tuple(by_file.get(file_path, ()))
                    targets = tuple(
                        nodeid
                        for nodeid in file_targets
                        if self._classified_nodeids.get(nodeid, tier) is tier
                    )
                    if not targets:
                        raise ValueError(
                            f"manifest file {file_path} has no canonically collected nodeids for {tier.value}"
                        )
                    measured_seconds = FILE_RUNTIME_SECONDS.get(file_path)
                    shard_count = (
                        min(len(targets), max(1, math.ceil(measured_seconds / TARGET_BATCH_SECONDS)))
                        if measured_seconds is not None
                        else 1
                    )
                    shard_size = math.ceil(len(targets) / shard_count)
                    for shard_index, offset in enumerate(
                        range(0, len(targets), shard_size),
                        start=1,
                    ):
                        shard = targets[offset : offset + shard_size]
                        weight = (
                            measured_seconds * len(shard) / len(file_targets)
                            if measured_seconds is not None
                            else float(len(shard))
                        )
                        planning_units.append((file_path, shard_index, shard, weight))

            bin_count = (
                1
                if not contract.parallel_safe
                else min(
                    min(self.jobs, contract.max_parallel_workers)
                    * (
                        INTEGRATION_BATCHES_PER_WORKER
                        if tier is TestTier.INTEGRATION_PROCESS
                        else BATCHES_PER_WORKER_AND_TIER
                    ),
                    len(planning_units),
                )
            )
            bins: list[list[tuple[str, int, tuple[str, ...], float]]] = [
                [] for _ in range(bin_count)
            ]
            bin_weights = [0.0] * bin_count
            for file_path, shard_index, targets, weight in sorted(
                planning_units,
                key=lambda item: (-item[3], item[0], item[1]),
            ):
                target_bin = min(range(bin_count), key=lambda index: (bin_weights[index], index))
                bins[target_bin].append((file_path, shard_index, targets, weight))
                bin_weights[target_bin] += weight

            for index, packed_files in enumerate(bins, start=1):
                packed_targets = tuple(
                    sorted(
                        (
                            nodeid
                            for _file_path, _shard_index, targets, _weight in packed_files
                            for nodeid in targets
                        ),
                        key=canonical_order.__getitem__,
                    )
                )
                execution.append(
                    Lane(
                        lane_id=f"{prefixes[tier]}-batch-{index:02d}",
                        tier=tier,
                        parallel_safe=contract.parallel_safe,
                        files=packed_targets,
                        timeout_seconds=contract.timeout_seconds,
                        estimated_seconds=bin_weights[index - 1],
                    )
                )
        flattened = tuple(target for lane in execution for target in lane.files)
        if Counter(flattened) != Counter(self._canonical_nodeids):
            raise ValueError("execution lane union does not match canonical collection")
        return tuple(execution)

    @staticmethod
    def _execution_plan_digest(lanes: Sequence[Lane]) -> str:
        payload = [lane.to_dict() for lane in lanes]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _validate_or_init_journal(
        self,
        execution_lanes: Sequence[Lane] | None = None,
    ) -> set[str]:
        """Validate journal identity if resuming; return set of previously passed lane_ids."""
        current_lanes = tuple(execution_lanes) if execution_lanes is not None else self.lanes
        plan_digest = self._execution_plan_digest(current_lanes)
        if not self.journal_path or not self.journal_path.exists():
            if self.resume:
                raise ValueError(
                    f"Resume requested but journal file '{self.journal_path}' does not exist."
                )
            if self.journal_path:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                self.journal_path.write_text(
                    json.dumps(
                        {
                            "event": "run_initialized",
                            "identity": self.identity.to_dict(),
                            "execution_plan_sha256": plan_digest,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            return set()

        if not self.resume:
            raise ValueError(
                f"Journal '{self.journal_path}' already exists; use a new path or pass --resume."
            )

        # Read journal
        lines = [line.strip() for line in self.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            if self.resume:
                raise ValueError("Journal file is empty; cannot resume.")
            return set()

        first = json.loads(lines[0])
        recorded_id = first.get("identity")
        if not recorded_id:
            raise ValueError("Malformed journal: missing run identity in header.")

        mismatches: list[str] = []
        current_dict = self.identity.to_dict()
        for key, curr_val in current_dict.items():
            rec_val = recorded_id.get(key)
            if rec_val != curr_val:
                mismatches.append(f"{key}: recorded '{rec_val}' vs current '{curr_val}'")
        if mismatches:
            raise ValueError(
                "Resume refused: journal identity does not match current run:\n  "
                + "\n  ".join(mismatches)
            )
        recorded_plan_digest = first.get("execution_plan_sha256")
        if recorded_plan_digest != plan_digest:
            raise ValueError(
                "Resume refused: execution plan does not match current lanes: "
                f"recorded '{recorded_plan_digest}' vs current '{plan_digest}'"
            )

        passed_lanes: set[str] = set()
        self._resumed_records.clear()
        for line in lines[1:]:
            record = json.loads(line)
            if record.get("event") == "lane_completed" and record.get("passed") is True:
                lane_id = str(record["lane_id"])
                required_counts = (
                    "passed_count",
                    "failed_count",
                    "skipped_count",
                    "subtests_passed",
                )
                if any(not isinstance(record.get(key), int) for key in required_counts):
                    raise ValueError(
                        f"Malformed journal: passed lane '{lane_id}' is missing integer test counts."
                    )
                duration_records = record.get("test_durations")
                if not isinstance(duration_records, list) or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("nodeid"), str)
                    or item.get("phase") not in {"setup", "call", "teardown"}
                    or not isinstance(item.get("duration_seconds"), (int, float))
                    for item in duration_records
                ):
                    raise ValueError(
                        f"Malformed journal: passed lane '{lane_id}' is missing valid test durations."
                    )
                passed_lanes.add(lane_id)
                self._resumed_records[lane_id] = record

        return passed_lanes

    def _append_journal(self, event_data: dict) -> None:
        """Atomically append a structured record to the journal file."""
        if not self.journal_path:
            return
        line = json.dumps(event_data) + "\n"
        with self._journal_lock, open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _execute_lane(self, lane: Lane, worker_id: int) -> LaneResult:
        """Run a single lane inside an isolated worker context."""
        ctx = self._pool_isolation.get_worker_context(worker_id)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--durations=0",
            "--durations-min=0",
            *lane.files,
        ]

        t_start = time.monotonic()
        timed_out = False
        error_msg = ""
        proc: subprocess.Popen[str] | None = None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.repo_root,
                env=ctx.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **_popen_session_kwargs(),
            )
            self._register_process(proc)
            stdout, stderr = proc.communicate(timeout=lane.timeout_seconds)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            error_msg = f"Lane exceeded timeout of {lane.timeout_seconds:.1f}s"
            if proc:
                kill_process_tree(proc.pid)
                try:
                    stdout, stderr = proc.communicate(timeout=5.0)
                except (OSError, subprocess.SubprocessError):
                    stdout, stderr = "", ""
            else:
                stdout, stderr = "", ""
            returncode = 124
        except (OSError, subprocess.SubprocessError) as exc:
            error_msg = f"Failed to spawn lane subprocess: {exc}"
            stdout, stderr = "", str(exc)
            returncode = 127
            if proc:
                kill_process_tree(proc.pid)
        finally:
            if proc is not None:
                self._unregister_process(proc)

        duration = time.monotonic() - t_start
        passed_count, failed_count, skipped_count, subtests_passed = parse_pytest_counts(stdout + stderr)
        test_durations = parse_pytest_durations(stdout + stderr)
        passed = (returncode == 0) and not timed_out

        result = LaneResult(
            lane_id=lane.lane_id,
            tier=str(lane.tier),
            passed=passed,
            returncode=returncode,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            worker_id=worker_id,
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            subtests_passed=subtests_passed,
            timed_out=timed_out,
            error_message=error_msg,
            test_durations=test_durations,
        )

        journal_record: dict[str, object] = {
            "event": "lane_completed",
            "lane_id": lane.lane_id,
            "tier": str(lane.tier),
            "passed": passed,
            "returncode": returncode,
            "duration_seconds": round(duration, 3),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "subtests_passed": subtests_passed,
            "timed_out": timed_out,
            "test_durations": [asdict(item) for item in test_durations],
        }
        if not passed:
            journal_record["stdout"] = _bounded_failure_output(stdout)
            journal_record["stderr"] = _bounded_failure_output(stderr)
            journal_record["error_message"] = error_msg
        self._append_journal(journal_record)

        return result

    def run(self) -> int:
        """Execute the regression orchestrator."""
        previous_break_handler: object | None = None
        if (
            platform.system() == "Windows"
            and threading.current_thread() is threading.main_thread()
        ):
            previous_break_handler = signal.getsignal(signal.SIGBREAK)

            def interrupt_parent(_signum: int, _frame: object) -> None:
                raise KeyboardInterrupt

            signal.signal(signal.SIGBREAK, interrupt_parent)
        try:
            self._run_lock.acquire()
        except RuntimeError as exc:
            print(f"Orchestrator configuration error: {exc}", file=sys.stderr)
            self._pool_isolation.cleanup()
            if previous_break_handler is not None:
                signal.signal(signal.SIGBREAK, previous_break_handler)
            return 2
        try:
            return self._run_locked()
        finally:
            self._run_lock.release()
            if previous_break_handler is not None:
                signal.signal(signal.SIGBREAK, previous_break_handler)

    def _run_locked(self) -> int:
        """Execute one identity-bound run while holding the repository run lock."""
        t_global_start = time.monotonic()
        try:
            collection = self._audit_collection()
            execution_lanes = self._build_execution_lanes()
            passed_from_journal = self._validate_or_init_journal(execution_lanes)
        except ValueError as exc:
            print(f"Orchestrator configuration error: {exc}", file=sys.stderr)
            self._pool_isolation.cleanup()
            return 2

        known_lane_ids = {lane.lane_id for lane in execution_lanes}
        unknown_resumed = sorted(passed_from_journal - known_lane_ids)
        if unknown_resumed:
            print(
                f"Orchestrator configuration error: journal contains unknown lanes: {unknown_resumed[:5]}",
                file=sys.stderr,
            )
            self._pool_isolation.cleanup()
            return 2

        lanes_to_run = [lane for lane in execution_lanes if lane.lane_id not in passed_from_journal]
        total_lanes = len(execution_lanes)
        resumed_count = len(passed_from_journal)

        print(
            f"StageMesh Regression Orchestrator\n"
            f"Commit: {self.identity.commit_sha[:10]} | OS: {self.identity.operating_system} | "
            f"Workers: {self.jobs} | External-process cap: "
            f"{TIER_ISOLATION_CONTRACTS[TestTier.INTEGRATION_PROCESS].max_parallel_workers} | "
            f"Total lanes: {total_lanes} "
            f"(running: {len(lanes_to_run)}, resumed: {resumed_count})\n"
            f"Collection: {collection['canonical_count']} nodeids | "
            f"sha256: {str(collection['canonical_sha256'])[:12]} | exact tier partition\n"
            f"{'='*78}", flush=True
        )

        results: list[LaneResult] = []
        # Pre-populate results for safely resumed lanes with their original counts.
        for lane in execution_lanes:
            if lane.lane_id in passed_from_journal:
                record = self._resumed_records[lane.lane_id]
                results.append(
                    LaneResult(
                        lane_id=lane.lane_id,
                        tier=str(lane.tier),
                        passed=True,
                        returncode=0,
                        duration_seconds=0.0,
                        stdout="[resumed from journal]",
                        stderr="",
                        worker_id=-1,
                        passed_count=int(record["passed_count"]),
                        failed_count=int(record["failed_count"]),
                        skipped_count=int(record["skipped_count"]),
                        subtests_passed=int(record["subtests_passed"]),
                        test_durations=tuple(
                            PytestDuration(
                                nodeid=str(item["nodeid"]),
                                phase=str(item["phase"]),
                                duration_seconds=float(item["duration_seconds"]),
                            )
                            for item in record["test_durations"]
                        ),
                    )
                )

        parallel_lanes = sorted(
            (lane for lane in lanes_to_run if lane.parallel_safe),
            key=lambda lane: len(lane.files),
            reverse=True,
        )
        serial_lanes = [l for l in lanes_to_run if not l.parallel_safe]

        completed_count = resumed_count
        interrupted = False

        def record_result(lane: Lane, result: LaneResult) -> None:
            nonlocal completed_count
            results.append(result)
            completed_count += 1
            status_str = "PASS" if result.passed else "FAIL"
            counts_str = (
                f"{result.passed_count} passed"
                + (f", {result.failed_count} failed" if result.failed_count else "")
                + (f", {result.skipped_count} skipped" if result.skipped_count else "")
            )
            with self._print_lock:
                print(
                    f"[{completed_count:03d}/{total_lanes:03d}] {status_str} "
                    f"tier:{lane.tier.value:<22} lane:{lane.lane_id:<24} "
                    f"({result.duration_seconds:5.1f}s) | {counts_str}",
                    flush=True,
                )

        def unexpected_result(lane: Lane, exc: BaseException) -> LaneResult:
            message = f"Unhandled lane worker error: {type(exc).__name__}: {exc}"
            self._append_journal(
                {
                    "event": "lane_completed",
                    "lane_id": lane.lane_id,
                    "tier": str(lane.tier),
                    "passed": False,
                    "returncode": 125,
                    "duration_seconds": 0.0,
                    "passed_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "subtests_passed": 0,
                    "timed_out": False,
                    "error_message": message,
                }
            )
            return LaneResult(
                lane_id=lane.lane_id,
                tier=str(lane.tier),
                passed=False,
                returncode=125,
                duration_seconds=0.0,
                stdout="",
                stderr="",
                worker_id=-1,
                error_message=message,
            )

        try:
            # 1. Execute parallel-safe file lanes.
            if parallel_lanes:
                available_workers_lock = threading.Lock()
                available_workers = list(range(self.jobs))

                def run_worker_task(lane: Lane) -> LaneResult:
                    with available_workers_lock:
                        worker_id = available_workers.pop()
                    try:
                        return self._execute_lane(lane, worker_id)
                    finally:
                        with available_workers_lock:
                            available_workers.append(worker_id)

                pool = ThreadPoolExecutor(max_workers=self.jobs)
                pending_lanes = list(parallel_lanes)
                future_map: dict[Future[LaneResult], Lane] = {}
                active_by_group: Counter[str] = Counter()

                def submit_available() -> None:
                    while pending_lanes and len(future_map) < self.jobs:
                        selected_index = next(
                            (
                                index
                                for index, candidate in enumerate(pending_lanes)
                                if active_by_group[
                                    TIER_ISOLATION_CONTRACTS[candidate.tier].concurrency_group
                                ]
                                < min(
                                    self.jobs,
                                    TIER_ISOLATION_CONTRACTS[candidate.tier].max_parallel_workers,
                                )
                            ),
                            None,
                        )
                        if selected_index is None:
                            return
                        lane = pending_lanes.pop(selected_index)
                        contract = TIER_ISOLATION_CONTRACTS[lane.tier]
                        active_by_group[contract.concurrency_group] += 1
                        future_map[pool.submit(run_worker_task, lane)] = lane

                submit_available()
                try:
                    while future_map:
                        completed, _pending = wait(
                            set(future_map),
                            timeout=0.2,
                            return_when=FIRST_COMPLETED,
                        )
                        for future in completed:
                            lane = future_map.pop(future)
                            contract = TIER_ISOLATION_CONTRACTS[lane.tier]
                            active_by_group[contract.concurrency_group] -= 1
                            try:
                                result = future.result()
                            except Exception as exc:  # noqa: BLE001 - worker boundary becomes evidence
                                result = unexpected_result(lane, exc)
                            record_result(lane, result)
                        submit_available()
                except KeyboardInterrupt:
                    interrupted = True
                    self._terminate_active_processes()
                    for future in future_map:
                        future.cancel()
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)

            # 2. Execute explicitly serial native lanes after parallel work.
            if not interrupted:
                for lane in serial_lanes:
                    try:
                        record_result(lane, self._execute_lane(lane, worker_id=0))
                    except KeyboardInterrupt:
                        interrupted = True
                        self._terminate_active_processes()
                        break
                    except Exception as exc:  # noqa: BLE001 - worker boundary becomes evidence
                        record_result(lane, unexpected_result(lane, exc))
        except KeyboardInterrupt:
            interrupted = True
            self._terminate_active_processes()
        finally:
            self._terminate_active_processes()
            self._pool_isolation.cleanup()

        total_wall_time = time.monotonic() - t_global_start
        failed_results = [r for r in results if not r.passed]

        # Aggregate test counts
        agg_passed = sum(r.passed_count for r in results)
        agg_failed = sum(r.failed_count for r in results)
        agg_skipped = sum(r.skipped_count for r in results)
        agg_subtests = sum(r.subtests_passed for r in results)
        accounted_nodeids = agg_passed + agg_failed + agg_skipped + agg_subtests
        expected_nodeids = int(collection["canonical_count"])
        coverage_matches = accounted_nodeids == expected_nodeids
        test_duration_totals: dict[str, float] = {}
        for result in results:
            for duration in result.test_durations:
                test_duration_totals[duration.nodeid] = (
                    test_duration_totals.get(duration.nodeid, 0.0)
                    + duration.duration_seconds
                )
        slowest_tests = [
            {"nodeid": nodeid, "duration_seconds": round(seconds, 6)}
            for nodeid, seconds in sorted(
                test_duration_totals.items(),
                key=lambda item: (-item[1], item[0]),
            )[:REPORT_SLOWEST_TEST_LIMIT]
        ]

        print(f"\n{'='*78}")
        if interrupted:
            verdict = "INTERRUPTED: active subprocesses terminated and partial evidence preserved"
        elif failed_results:
            print(f"FAILURES DETECTED ({len(failed_results)} failed lanes):\n")
            for r in failed_results:
                print(f"--- FAILED: lane {r.lane_id} ({r.tier}) ---")
                if r.error_message:
                    print(f"Error: {r.error_message}")
                print(r.stdout)
                print(r.stderr)
                print("-" * 50)
            verdict = f"FAILED: {len(failed_results)} of {total_lanes} lanes failed"
        elif not coverage_matches:
            verdict = (
                "FAILED: result accounting does not match canonical collection "
                f"({accounted_nodeids} accounted vs {expected_nodeids} collected)"
            )
        else:
            verdict = f"PASSED: all {total_lanes} lanes passed"

        print(
            f"\n{verdict}\n"
            f"Total Tests: {agg_passed} passed, {agg_skipped} skipped, {agg_subtests} subtests passed\n"
            f"Coverage: {accounted_nodeids}/{expected_nodeids} collected nodeids accounted\n"
            f"Wall clock duration: {total_wall_time:.2f}s ({total_wall_time/60.0:.2f} minutes)\n"
            f"{'='*78}", flush=True
        )

        # Write machine-readable report if requested
        if self.report_path:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            report_data = {
                "identity": self.identity.to_dict(),
                "collection": collection,
                "verdict": (
                    "INTERRUPTED"
                    if interrupted
                    else "PASSED"
                    if not failed_results and coverage_matches
                    else "FAILED"
                ),
                "wall_time_seconds": round(total_wall_time, 3),
                "manifest_lanes": len(self.lanes),
                "total_lanes": total_lanes,
                "execution_plan_sha256": self._execution_plan_digest(execution_lanes),
                "execution_plan": [lane.to_dict() for lane in execution_lanes],
                "failed_lanes": len(failed_results),
                "aggregate_counts": {
                    "passed": agg_passed,
                    "failed": agg_failed,
                    "skipped": agg_skipped,
                    "subtests_passed": agg_subtests,
                },
                "coverage": {
                    "accounted_nodeids": accounted_nodeids,
                    "expected_nodeids": expected_nodeids,
                    "matches": coverage_matches,
                },
                "slowest_tests": slowest_tests,
                "lanes": [r.to_dict() for r in sorted(results, key=lambda item: item.lane_id)],
            }
            self.report_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

        if interrupted:
            return 130
        return 1 if failed_results or not coverage_matches else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", "-j", type=int, help="Number of concurrent worker subprocesses")
    parser.add_argument("--tier", choices=[t.value for t in TestTier] + ["full"], default="full", help="Filter by tier")
    parser.add_argument("--journal", type=Path, help="Path to append-only incremental journal")
    parser.add_argument("--resume", action="store_true", help="Resume passed lanes from matching journal")
    parser.add_argument("--report", type=Path, help="Path for machine-readable JSON outcome evidence")
    args = parser.parse_args(argv)

    tier_filter = None if args.tier == "full" else args.tier
    orchestrator = RegressionOrchestrator(
        jobs=args.jobs,
        journal_path=args.journal,
        resume=args.resume,
        report_path=args.report,
        selected_tier=tier_filter,
    )
    return orchestrator.run()


if __name__ == "__main__":
    raise SystemExit(main())

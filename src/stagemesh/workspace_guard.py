"""Execution-owned task worktrees and detection of external mutation.

A task's worktree is writable by exactly one StageMesh execution at a time. The execution takes an exclusive *lease* (an owner file in the
worktree's private git dir, created with O_EXCL), and every authorized outcome is *sealed* into a ledger beside it: the HEAD, a
fingerprint of any uncommitted files, and the candidate SHA that execution produced. Everything StageMesh later does with that candidate
(the next implementation attempt, validation, review, integration) first compares the worktree and the candidate rows with the ledger.
Any difference that no active, authorized execution explains is an external mutation: StageMesh records a deterministic
`EXTERNAL_WORKSPACE_MUTATION` audit event and raises `WorkspaceMutation`, and the caller stops and blocks the task instead of adopting it.

What counts as authorized: the working-tree edits and linear fast-forward commits the lease holder's agent leaves behind when its process
exits (agents are told to commit). Git cannot tell an agent commit from another process's commit made *while that agent is running*, so
that window is bounded rather than closed: the lease excludes other StageMesh executions, the tree must be exactly as sealed before the
agent starts, HEAD must still descend from the sealed HEAD afterwards, and the result is sealed as the one candidate every later stage must
match. Nothing here writes to the database beyond audit events; state lives in the worktree's git dir and is removed with the worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .audit import record_audit
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, ProcessIdentity
from .git import GitError, GitWorkspace
from .persistence import Store
from .process_identity import classify_process, process_identity
from .workspaces import prepare_task_workspace, task_workspace

EXTERNAL_WORKSPACE_MUTATION = "EXTERNAL_WORKSPACE_MUTATION"
OWNERSHIP_INITIALIZED = "workspace.ownership_initialized"
LEASE_RECOVERED = "workspace.lease_recovered"
OWNER_FILE = "stagemesh-owner.json"
LEDGER_FILE = "stagemesh-workspace.json"
OWNER_MUTEX_FILE = "stagemesh-owner.lock"
OWNER_MUTEX_TIMEOUT = 10.0  # seconds a claim waits for another claim of the same workspace to finish
_RELEASE_RETRIES = 10
_RELEASE_RETRY_INTERVAL = 0.05
LEDGER_VERSION = 1
MAX_FINGERPRINT_PATHS = 500  # beyond this only the digest is kept, so the changed paths cannot be listed
MAX_REPORTED_PATHS = 20

REMEDY = (
    "inspect the workspace; either reset it to expected_sha (git reset --hard <expected_sha> && git clean -fd) or remove it "
    "(git worktree remove --force <workspace>), then run `stagemesh retry-task`. StageMesh will not adopt the change."
)


class WorkspaceMutation(RuntimeError):
    """The worktree, its owner or the candidate provenance is not what an authorized execution left. `detail` is the audit payload."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(
            f"{EXTERNAL_WORKSPACE_MUTATION} at {detail.get('stage')}: {detail.get('reason')} "
            f"(expected {detail.get('expected_sha')}, observed {detail.get('observed_sha')})"
        )

    @property
    def reason(self) -> str:
        return str(self.detail.get("reason"))




class WorkspaceReleaseError(RuntimeError):
    """The lease could not be released because its ownership file could not be unlinked."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(
            f"workspace lease release failed at {detail.get('workspace')}: {detail.get('reason')} "
            f"(token {detail.get('token')})"
        )

    @property
    def reason(self) -> str:
        return str(self.detail.get("reason"))
# --- observation ---------------------------------------------------------------------------------------------------------


def _content_hash(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        if not path.is_file():
            return "absent"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "unreadable"


class UndecodableGitOutput(GitError):
    """git wrote output that could not be decoded as UTF-8, so a path or id in it cannot be trusted."""


def _run(path: Path, *args: str, check: bool = True) -> Any:
    """Run git decoding its output as UTF-8 whatever the process locale is.

    git writes paths as UTF-8, so the locale codec (cp1252 on many Windows installs) turns a non-ASCII path into mojibake or, worse,
    makes subprocess return stdout=None with exit code 0. Both would let a changed file be fingerprinted under the wrong name or not at
    all, so output that does not decode, or comes back as None, is an error here and never an empty or lossy result.
    """
    try:
        result = GitWorkspace(path).run(*args, check=check, encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UndecodableGitOutput(f"git {args[0]} output is not valid UTF-8: {exc}") from exc
    if result.stdout is None or result.stderr is None:
        raise UndecodableGitOutput(f"git {args[0]} output could not be decoded as UTF-8")
    return result


def observe(path: Path) -> dict[str, Any]:
    """HEAD plus a fingerprint of every uncommitted path (modified, staged, deleted and untracked) in a worktree."""
    head = _run(path, "rev-parse", "HEAD").stdout.strip()
    status = _run(path, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
    entries = [entry for entry in status.split("\0") if len(entry) > 3]
    dirty = {entry[3:]: _content_hash(path / entry[3:]) for entry in entries}
    tracked_dirty = sorted(entry[3:] for entry in entries if not entry.startswith("??"))  # modified, staged or deleted tracked paths
    digest = hashlib.sha256(json.dumps(sorted(dirty.items())).encode("utf-8")).hexdigest()
    return {
        "head": head,
        "dirty": dirty if len(dirty) <= MAX_FINGERPRINT_PATHS else {},
        "dirty_count": len(dirty),
        "dirty_digest": digest,
        "tracked_dirty": tracked_dirty[:MAX_REPORTED_PATHS],
    }


def _changed_paths(sealed: dict[str, Any], seen: dict[str, Any]) -> list[str]:
    before, after = sealed.get("dirty") or {}, seen.get("dirty") or {}
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))[:MAX_REPORTED_PATHS]


def _difference(ledger: dict[str, Any], seen: dict[str, Any]) -> tuple[str, list[str]] | None:
    if seen["head"] != ledger["head"]:
        return "head_changed", []
    if seen["dirty_digest"] != ledger["dirty_digest"]:
        return "working_tree_modified", _changed_paths(ledger, seen)
    return None


# --- files in the worktree's git dir --------------------------------------------------------------------------------------


def _gitdir(path: Path) -> Path:
    return Path(_run(path, "rev-parse", "--absolute-git-dir").stdout.strip())


def _atomic_write(target: Path, payload: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    """The parsed file, None when absent; ValueError when present but unusable (fail closed, never treated as absent)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{path.name} is unreadable: {exc}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path.name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not an object")  # noqa: TRY004 - callers handle ValueError uniformly
    return value


def _owner_is_active(owner: dict[str, Any]) -> bool:
    saved = ProcessIdentity(owner.get("pid"), owner.get("create_time"), owner.get("boot_id"), owner.get("executable"))
    return classify_process(saved, process_identity(saved.pid)) != "DEAD"  # an unverifiable owner is treated as alive


def _self_owner(token: str, task_id: str, kind: str, claim_id: str | None) -> dict[str, Any]:
    me = process_identity(os.getpid())
    return {
        "token": token,
        "task_id": task_id,
        "kind": kind,
        "claim_id": claim_id,
        "execution_id": None,
        "pid": os.getpid(),
        "create_time": me.create_time if me else None,
        "boot_id": me.boot_id if me else None,
        "executable": me.executable if me else None,
        "acquired_at": time.time(),
    }


def _paths_between(workspace: Path, expected_sha: str, observed_sha: str) -> list[str]:
    """Files that differ between the sealed commit and the commit now at HEAD, for the audit record (best effort)."""
    try:
        result = _run(workspace, "diff", "--name-only", "-z", expected_sha, observed_sha, check=False)
    except (GitError, OSError):
        return []
    return sorted(name for name in result.stdout.split("\0") if name)[:MAX_REPORTED_PATHS]


# --- reporting -----------------------------------------------------------------------------------------------------------


def _mutation(
    store: Store,
    *,
    task_id: str,
    stage: str,
    reason: str,
    workspace: Path | None,
    expected_sha: str | None,
    observed_sha: str | None,
    execution_id: str | None = None,
    claim_id: str | None = None,
    candidate_sha: str | None = None,
    changed_paths: Sequence[str] = (),
    detail: str | None = None,
) -> WorkspaceMutation:
    """Record the deterministic audit event (fixed keys, no timestamps in the payload) and return the exception to raise."""
    if not changed_paths and reason == "head_changed" and workspace is not None and expected_sha and observed_sha:
        changed_paths = _paths_between(workspace, expected_sha, observed_sha)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "execution_id": execution_id,
        "claim_id": claim_id,
        "workspace": str(workspace) if workspace is not None else None,
        "stage": stage,
        "reason": reason,
        "expected_sha": expected_sha,
        "observed_sha": observed_sha,
        "candidate_sha": candidate_sha,
        "changed_paths": list(changed_paths)[:MAX_REPORTED_PATHS],
        "detail": detail,
        "remedy": REMEDY,
    }
    record_audit(store, EXTERNAL_WORKSPACE_MUTATION, payload)
    return WorkspaceMutation(payload)


# --- the lease an execution holds ----------------------------------------------------------------------------------------


class WorkspaceLease:
    def __init__(self, store: Store, task_id: str, kind: str, path: Path, gitdir: Path, token: str, claim_id: str | None, recovering: bool):
        self.store = store
        self.task_id = task_id
        self.kind = kind
        self.path = path
        self.claim_id = claim_id
        self.execution_id: str | None = None
        self._gitdir = gitdir
        self._token = token
        self._recovering = recovering
        self._ledger: dict[str, Any] = {}
        self._ledger_bytes = b""
        self._agent_ran = False
        self._window_open = False
        self._seal_error: Exception | None = None
        self._sealed = False
        self._released = False

    # -- identity -------------------------------------------------------------------------------------------------------

    @property
    def ledger(self) -> dict[str, Any]:
        return dict(self._ledger)

    def bind_execution(self, execution_id: str) -> None:
        self.execution_id = execution_id
        owner = self._read_owner()
        if owner is not None and owner.get("token") == self._token:
            _atomic_write(self._gitdir / OWNER_FILE, {**owner, "execution_id": execution_id})

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            return _read_json(self._gitdir / OWNER_FILE)
        except ValueError:
            return None

    def _fail(self, stage: str, reason: str, *, observed: str | None, expected: str | None = None, paths: Sequence[str] = (), detail: str | None = None) -> WorkspaceMutation:
        return _mutation(
            self.store,
            task_id=self.task_id,
            stage=f"{self.kind}:{stage}",
            reason=reason,
            workspace=self.path,
            expected_sha=expected if expected is not None else self._ledger.get("head"),
            observed_sha=observed,
            execution_id=self.execution_id,
            claim_id=self.claim_id,
            candidate_sha=self._ledger.get("candidate"),
            changed_paths=paths,
            detail=detail,
        )

    # -- checks ---------------------------------------------------------------------------------------------------------

    def _check_ownership(self, stage: str, observed_head: str | None) -> None:
        try:
            owner = _read_json(self._gitdir / OWNER_FILE)
            ledger_bytes = (self._gitdir / LEDGER_FILE).read_bytes()
        except (ValueError, OSError) as exc:
            raise self._fail(stage, "ownership_record_unreadable", observed=observed_head, detail=str(exc)) from exc
        if owner is None or owner.get("token") != self._token:
            raise self._fail(stage, "workspace_ownership_lost", observed=observed_head, detail=f"owner is now {(owner or {}).get('execution_id') or (owner or {}).get('claim_id')}")
        if ledger_bytes != self._ledger_bytes:
            raise self._fail(stage, "ownership_ledger_changed", observed=observed_head)

    def _observe(self, stage: str) -> dict[str, Any]:
        try:
            return observe(self.path)
        except UndecodableGitOutput as exc:
            raise self._fail(stage, "git_output_undecodable", observed=None, detail=str(exc)) from exc

    def check(self, stage: str) -> None:
        """The worktree is exactly what was sealed (HEAD and every uncommitted path) and this lease still owns it."""
        seen = self._observe(stage)
        self._check_ownership(stage, seen["head"])
        difference = _difference(self._ledger, seen)
        if difference is not None:
            raise self._fail(stage, difference[0], observed=seen["head"], paths=difference[1])
        if stage == "before_agent":
            self._window_open = True

    def after_agent(self) -> None:
        """The agent has exited: its output is authorized if HEAD is the sealed HEAD or a linear descendant of it and the lease held."""
        self._agent_ran = True
        seen = self._observe("after_agent")
        self._check_ownership("after_agent", seen["head"])
        sealed = self._ledger["head"]
        if seen["head"] != sealed and _run(self.path, "merge-base", "--is-ancestor", sealed, seen["head"], check=False).returncode != 0:
            raise self._fail("after_agent", "head_not_descendant", observed=seen["head"], detail="HEAD was moved off the sealed commit's history")

    # -- sealing --------------------------------------------------------------------------------------------------------

    def seal(self, candidate_sha: str | None = None) -> None:
        """Record the current worktree as the authorized result of this execution, and `candidate_sha` as the candidate it produced."""
        seen = self._observe("seal")
        candidate = candidate_sha if candidate_sha is not None else self._ledger.get("candidate")
        self._write_ledger(seen, candidate)
        self._sealed = True

    def _write_ledger(self, seen: dict[str, Any], candidate: str | None, **extra: Any) -> None:
        ledger = {
            "version": LEDGER_VERSION,
            "task_id": self.task_id,
            "head": seen["head"],
            "dirty": seen["dirty"],
            "dirty_count": seen["dirty_count"],
            "dirty_digest": seen["dirty_digest"],
            "candidate": candidate,
            "sealed_by": self.execution_id,
            "sealed_kind": self.kind,
            **extra,
        }
        _atomic_write(self._gitdir / LEDGER_FILE, ledger)
        self._ledger = ledger
        self._ledger_bytes = (self._gitdir / LEDGER_FILE).read_bytes()

    # -- release --------------------------------------------------------------------------------------------------------

    def release(self) -> None:
        if self._released:
            return
        target = self._gitdir / OWNER_FILE
        last_error: OSError | None = None

        with _owner_mutex(self._gitdir) as locked:
            if not locked:
                detail = {
                    "task_id": self.task_id,
                    "workspace": str(self.path),
                    "token": self._token,
                    "reason": "owner_mutex_timeout",
                    "execution_id": self.execution_id,
                    "claim_id": self.claim_id,
                }
                raise WorkspaceReleaseError(detail)

            for attempt in range(_RELEASE_RETRIES):
                if not target.exists():
                    self._released = True
                    return

                try:
                    owner = _read_json(target)
                except ValueError:
                    owner = {"token": self._token}  # corrupt record on disk; attempt removal
                except OSError as exc:
                    last_error = exc
                    time.sleep(_RELEASE_RETRY_INTERVAL)
                    continue

                if owner is not None and owner.get("token") != self._token:
                    # Foreign owner: do not delete another claimant's record
                    self._released = True
                    return

                try:
                    os.unlink(target)
                    self._released = True
                    return
                except FileNotFoundError:
                    self._released = True
                    return
                except OSError as exc:
                    last_error = exc
                    if attempt < _RELEASE_RETRIES - 1:
                        time.sleep(_RELEASE_RETRY_INTERVAL)

        detail = {
            "task_id": self.task_id,
            "workspace": str(self.path),
            "token": self._token,
            "reason": "owner_file_unlink_failed",
            "error": str(last_error),
            "execution_id": self.execution_id,
            "claim_id": self.claim_id,
        }
        raise WorkspaceReleaseError(detail)

def _load_ledger(gitdir: Path) -> tuple[dict[str, Any] | None, bytes]:
    data = _read_json(gitdir / LEDGER_FILE)
    if data is None:
        return None, b""
    for key in ("head", "dirty_digest", "dirty", "candidate"):
        if key not in data:
            raise ValueError(f"{LEDGER_FILE} is missing {key}")
    return data, (gitdir / LEDGER_FILE).read_bytes()


def _try_lock(handle: Any) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: Any) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _owner_mutex(gitdir: Path, timeout: float = OWNER_MUTEX_TIMEOUT) -> Iterator[bool]:
    """Serializes every claim (and so every dead-owner takeover) of one workspace, across threads and processes.

    An OS file lock, so the kernel releases it if the holder dies: there is no stale mutex to recover from, and who wins a takeover is
    decided by the lock, not by timing. Yields False when the lock could not be had within `timeout` (a claim that waits that long is
    refused, which is fail-closed but does depend on the clock); the caller then refuses rather than guess.
    """
    handle = open(gitdir / OWNER_MUTEX_FILE, "a+b")  # noqa: SIM115 - closed in the finally below
    try:
        deadline = time.monotonic() + timeout
        locked = _try_lock(handle)
        while not locked and time.monotonic() < deadline:
            time.sleep(0.01)
            locked = _try_lock(handle)
        try:
            yield locked
        finally:
            if locked:
                _unlock(handle)
    finally:
        handle.close()


def _claim(store: Store, task_id: str, kind: str, path: Path, gitdir: Path, token: str, claim_id: str | None) -> bool:
    """Take the owner file. Returns True when a dead owner's stale file was replaced; refuses a live or unreadable owner.

    The whole read-judge-replace sequence runs under the workspace's owner mutex, so two executions that both see the same dead owner cannot
    both take over: the second one waits, then finds the first one's live record and is refused.
    """
    with _owner_mutex(gitdir) as locked:
        if not locked:
            raise _mutation(store, task_id=task_id, stage=f"{kind}:acquire", reason="workspace_owned_by_another_execution", workspace=path, expected_sha=None, observed_sha=None, claim_id=claim_id, detail="another execution is claiming this workspace and did not finish in time")
        return _claim_locked(store, task_id, kind, path, gitdir, token, claim_id)


def _safe_cleanup_created_owner(target: Path, token: str) -> None:
    """Safely unlink an owner file created during a failed acquisition attempt.

    Never deletes another claimant's valid owner file:
    - If target does not exist, returns immediately.
    - If target has valid content belonging to another token, it is preserved.
    - If target is empty, corrupt from this failed write, or matches our token, it is unlinked.
    - If cleanup fails after retries, raises WorkspaceReleaseError fail-closed.
    """
    last_error: Exception | None = None
    for attempt in range(5):
        if not target.exists():
            return
        try:
            data = _read_json(target)
            if isinstance(data, dict) and data.get("token") != token:
                return  # foreign owner record, do not delete
        except ValueError as exc:
            last_error = exc
        try:
            os.unlink(target)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(0.02)
    if target.exists():
        raise WorkspaceReleaseError({
            "workspace": str(target.parent),
            "token": token,
            "reason": "owner_file_cleanup_failed",
            "error": str(last_error),
        })


def _claim_locked(store: Store, task_id: str, kind: str, path: Path, gitdir: Path, token: str, claim_id: str | None) -> bool:
    target = gitdir / OWNER_FILE
    recovered = False
    for _attempt in range(2):
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner = _read_json(target)
            except ValueError as exc:
                raise _mutation(store, task_id=task_id, stage=f"{kind}:acquire", reason="ownership_record_unreadable", workspace=path, expected_sha=None, observed_sha=None, claim_id=claim_id, detail=str(exc)) from exc
            if owner is not None and _owner_is_active(owner):
                raise _mutation(
                    store,
                    task_id=task_id,
                    stage=f"{kind}:acquire",
                    reason="workspace_owned_by_another_execution",
                    workspace=path,
                    expected_sha=None,
                    observed_sha=None,
                    execution_id=owner.get("execution_id"),
                    claim_id=claim_id,
                    detail=f"owned by {owner.get('kind')} execution {owner.get('execution_id') or owner.get('claim_id')} (pid {owner.get('pid')})",
                ) from None
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
            recovered = recovered or owner is not None
            continue

        created = True
        try:
            try:
                owner_data = _self_owner(token, task_id, kind, claim_id)
                payload = json.dumps(owner_data, sort_keys=True) + "\n"
            except BaseException:
                os.close(fd)
                fd = None
                raise

            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return recovered
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if created:
                _safe_cleanup_created_owner(target, token)
            raise
    raise _mutation(store, task_id=task_id, stage=f"{kind}:acquire", reason="workspace_owned_by_another_execution", workspace=path, expected_sha=None, observed_sha=None, claim_id=claim_id, detail="lost the race for the owner file")


def _latest_candidate_sha(store: Store, task_id: str) -> str | None:
    row = store.latest_candidate(task_id)
    return str(row["sha"]) if row is not None else None


def _anchor_sha(store: Store, task_id: str) -> str | None:
    """What the database says an unrecorded worktree's HEAD must be: the latest candidate, else the task baseline."""
    return _latest_candidate_sha(store, task_id) or store.task_baseline(task_id)


def acquire_workspace(store: Store, project: Path, task_id: str, kind: str, *, claim_id: str | None = None) -> WorkspaceLease:
    """Create or reuse the task's worktree, take its exclusive lease and prove it is exactly what the last authorized execution sealed."""
    root = Path(project).resolve()
    GitWorkspace(root).init_if_needed()  # as prepare_task_workspace does first: the project may not exist yet, and the config read below needs it
    existed = (task_workspace(root, task_id) / ".git").exists()
    path = prepare_task_workspace(root, task_id)
    try:
        gitdir = _gitdir(path)
    except UndecodableGitOutput as exc:
        raise _mutation(store, task_id=task_id, stage=f"{kind}:acquire", reason="git_output_undecodable", workspace=path, expected_sha=None, observed_sha=None, claim_id=claim_id, detail=str(exc)) from exc
    token = uuid.uuid4().hex
    recovered = _claim(store, task_id, kind, path, gitdir, token, claim_id)
    lease: WorkspaceLease | None = None
    try:
        lease = WorkspaceLease(store, task_id, kind, path, gitdir, token, claim_id, recovered)
        _establish(lease, existed)
        return lease
    except BaseException:
        if lease is not None:
            lease.release()
        else:
            with _owner_mutex(gitdir) as locked:
                if not locked:
                    raise WorkspaceReleaseError({
                        "task_id": task_id,
                        "workspace": str(path),
                        "token": token,
                        "reason": "owner_mutex_timeout",
                    })
                _safe_cleanup_created_owner(gitdir / OWNER_FILE, token)
        raise
def _establish(lease: WorkspaceLease, existed: bool) -> None:
    store, task_id = lease.store, lease.task_id
    try:
        ledger, raw = _load_ledger(lease._gitdir)
    except ValueError as exc:
        raise lease._fail("acquire", "ownership_record_unreadable", observed=None, detail=str(exc)) from exc
    seen = lease._observe("acquire")
    latest = _latest_candidate_sha(store, task_id)
    if ledger is None:
        anchor = _anchor_sha(store, task_id)
        if existed and anchor is not None and seen["head"] != anchor:
            raise lease._fail("acquire", "no_ownership_record_and_head_differs", observed=seen["head"], expected=anchor)
        if existed and seen["tracked_dirty"]:
            raise lease._fail("acquire", "legacy_workspace_has_uncommitted_changes", observed=seen["head"], expected=anchor or seen["head"], paths=seen["tracked_dirty"], detail="a workspace with no ownership record is adopted only when its tracked files are clean")
        lease._ledger, lease._ledger_bytes = {"head": seen["head"], "candidate": latest}, b""
        lease._write_ledger(seen, latest)
        record_audit(store, OWNERSHIP_INITIALIZED, {"task_id": task_id, "workspace": str(lease.path), "head": seen["head"], "adopted_existing": existed, "kind": lease.kind})
        return
    lease._ledger, lease._ledger_bytes = ledger, raw
    if ledger.get("task_id") not in (None, task_id):
        raise lease._fail("acquire", "workspace_belongs_to_another_task", observed=seen["head"], detail=str(ledger.get("task_id")))
    difference = _difference(ledger, seen)
    if difference is not None:
        raise lease._fail("acquire", difference[0], observed=seen["head"], paths=difference[1])
    if ledger.get("candidate") != latest:
        raise lease._fail("acquire", "candidate_provenance_changed", observed=latest, expected=ledger.get("candidate"))
    if lease._recovering:
        # The previous owner died, but the workspace is exactly what it last sealed, so taking it over adopts nothing. Anything else was
        # refused above as an external mutation: recovery never accepts a difference.
        record_audit(store, LEASE_RECOVERED, {"task_id": task_id, "workspace": str(lease.path), "sealed_head": ledger["head"], "observed_head": seen["head"], "kind": lease.kind})


@contextmanager
def owned_workspace(store: Store, project: Path, task_id: str, kind: str, *, claim_id: str | None = None) -> Iterator[WorkspaceLease]:
    """`with owned_workspace(...) as lease:` holds the lease for the block. A mutation fails the execution; any other exit keeps state honest.

    Once the lease is taken it is released by `finally`, so no exit path (success, an error in the block, a failing seal, a failing audit
    write, Ctrl+C) can leave the workspace owned by a process that is no longer using it.
    """
    lease = acquire_workspace(store, project, task_id, kind, claim_id=claim_id)
    try:
        try:
            yield lease
            if not lease._sealed:
                _seal_or_fail(lease)
        except WorkspaceMutation:
            _fail_execution(lease)
            raise
        except BaseException:
            # After the before-agent check, an exception or Ctrl+C seals what this execution left.
            # Before that check, a difference is not the agent's output and must not be sealed as authorized.
            if lease._window_open:
                lease._agent_ran = True
            try:
                _seal_or_fail(lease)
            except WorkspaceMutation:
                _fail_execution(lease)
                raise
            except Exception as exc:  # noqa: BLE001 - sealing failed for a non-integrity reason; the caller needs the original error, and the
                lease._seal_error = exc  # unsealed ledger makes the next claim compare against the old seal and fail closed if anything changed
            raise
    finally:
        lease.release()


def _seal_or_fail(lease: WorkspaceLease) -> None:
    """The block ended without sealing (a failed or empty attempt): the state must still be explained by this execution."""
    seen = lease._observe("release")
    lease._check_ownership("release", seen["head"])
    descends = lease._agent_ran and _run(lease.path, "merge-base", "--is-ancestor", lease._ledger["head"], seen["head"], check=False).returncode == 0
    if seen["head"] != lease._ledger["head"] and not descends:
        raise lease._fail("release", "head_changed", observed=seen["head"])
    if not lease._agent_ran:
        difference = _difference(lease._ledger, seen)
        if difference is not None:
            raise lease._fail("release", difference[0], observed=seen["head"], paths=difference[1])
    lease._write_ledger(seen, lease._ledger.get("candidate"))


def _fail_execution(lease: WorkspaceLease) -> None:
    if lease.execution_id is None:
        return
    row = lease.store.conn.execute("SELECT status FROM executions WHERE id=?", (lease.execution_id,)).fetchone()
    if row is not None and row["status"] == ExecutionStatus.RUNNING:
        lease.store.finish_execution(lease.execution_id, ExecutionStatus.FAILED, result="external_workspace_mutation")


# --- stage boundaries (no lease: validation, review and integration never write the implementation worktree) ----------------


def pin_candidate(project: Path, task_id: str, candidate_sha: str) -> None:
    """An operator action registered `candidate_sha`; record it in the ledger so it is the candidate every later check expects."""
    root = Path(project).resolve()
    if not root.is_dir():
        return  # no project checkout, so no task worktree to pin
    target = task_workspace(root, task_id)
    if not (target / ".git").exists():
        return
    gitdir = _gitdir(target)
    try:
        ledger, _raw = _load_ledger(gitdir)
    except ValueError:
        return  # an unreadable ledger is reported by the next check; an operator action must not hide it
    if ledger is not None:
        _atomic_write(gitdir / LEDGER_FILE, {**ledger, "candidate": candidate_sha})


def verify_candidate_workspace(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str,
    stage: str,
    *,
    require: Sequence[EvidenceKind] = (),
) -> None:
    """Prove, at a lifecycle boundary, that `candidate_sha` is exactly what the authorized execution produced. Raises WorkspaceMutation."""
    root = Path(project).resolve()
    target = task_workspace(root, task_id) if root.is_dir() else root  # a project that does not exist yet owns no worktree
    owned = root.is_dir() and (target / ".git").exists()
    ledger: dict[str, Any] | None = None
    seen: dict[str, Any] | None = None

    def fail(reason: str, *, expected: str | None, observed: str | None, paths: Sequence[str] = (), detail: str | None = None) -> WorkspaceMutation:
        return _mutation(
            store,
            task_id=task_id,
            stage=stage,
            reason=reason,
            workspace=target if owned else None,
            expected_sha=expected,
            observed_sha=observed,
            execution_id=(ledger or {}).get("sealed_by"),
            candidate_sha=candidate_sha,
            changed_paths=paths,
            detail=detail,
        )

    if owned:
        try:
            gitdir = _gitdir(target)
        except UndecodableGitOutput as exc:
            raise fail("git_output_undecodable", expected=None, observed=None, detail=str(exc)) from exc
        try:
            ledger, _raw = _load_ledger(gitdir)
            owner = _read_json(gitdir / OWNER_FILE)
        except ValueError as exc:
            raise fail("ownership_record_unreadable", expected=None, observed=None, detail=str(exc)) from exc
        try:
            seen = observe(target)
        except UndecodableGitOutput as exc:
            raise fail("git_output_undecodable", expected=None, observed=None, detail=str(exc)) from exc
        if owner is not None and _owner_is_active(owner):
            raise fail("workspace_owned_by_another_execution", expected=(ledger or {}).get("head"), observed=seen["head"], detail=f"{owner.get('kind')} execution {owner.get('execution_id') or owner.get('claim_id')} holds the lease")
        if ledger is None:
            anchor = _anchor_sha(store, task_id)
            if anchor is not None and seen["head"] != anchor:
                raise fail("no_ownership_record_and_head_differs", expected=anchor, observed=seen["head"])
            if seen["tracked_dirty"]:
                raise fail("legacy_workspace_has_uncommitted_changes", expected=anchor or seen["head"], observed=seen["head"], paths=seen["tracked_dirty"])
            ledger = {"head": seen["head"], "dirty_digest": seen["dirty_digest"], "dirty": seen["dirty"], "candidate": _latest_candidate_sha(store, task_id), "sealed_by": None}
        else:
            difference = _difference(ledger, seen)
            if difference is not None:
                raise fail(difference[0], expected=ledger["head"], observed=seen["head"], paths=difference[1])
    expected_candidate = (ledger or {}).get("candidate")
    latest = _latest_candidate_sha(store, task_id)
    if (expected_candidate is not None and expected_candidate != candidate_sha) or (latest is not None and latest != candidate_sha):
        raise fail("candidate_provenance_changed", expected=expected_candidate or latest, observed=candidate_sha if expected_candidate != candidate_sha else latest)
    for kind in require:
        if not store.has_evidence(task_id, candidate_sha, kind, EvidenceStatus.PASSED):
            raise fail(f"candidate_without_{str(kind).lower()}_evidence", expected=expected_candidate or candidate_sha, observed=candidate_sha, detail=f"no PASSED {kind} evidence is recorded for exactly this candidate")


__all__ = [
    "EXTERNAL_WORKSPACE_MUTATION",
    "WorkspaceLease",
    "WorkspaceMutation",
    "WorkspaceReleaseError",
    "acquire_workspace",
    "observe",
    "owned_workspace",
    "pin_candidate",
    "verify_candidate_workspace",
]

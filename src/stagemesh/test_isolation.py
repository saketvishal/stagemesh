"""Hermetic worker isolation contracts and environment management for StageMesh tests.

Guarantees that parallel test workers cannot conflict on:
- Temporary directories and filesystem state
- Git configurations and global credentials
- SQLite database paths
- Loopback network ports
- Ambient provider environment variables and secrets
- Child process lifecycle and tree cleanup
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


class RegressionRunLock:
    """Cross-process lock preventing overlapping full runs for one Git repository."""

    def __init__(self, repo_root: Path) -> None:
        common_dir = self._git_common_dir(repo_root)
        identity = str(common_dir).casefold() if platform.system() == "Windows" else str(common_dir)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / "stagemesh-regression-locks" / f"{digest}.lock"
        self._handle = None

    @staticmethod
    def _git_common_dir(repo_root: Path) -> Path:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                common = Path(result.stdout.strip())
                return (repo_root / common).resolve() if not common.is_absolute() else common.resolve()
        except (OSError, subprocess.SubprocessError):
            pass
        return repo_root.resolve()

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("regression run lock is already held by this process")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - handle owns the lock lifetime
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"another StageMesh regression run already holds {self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        handle.seek(0)
        try:
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _remove_readonly(func, path, exc_info):
    """Clear readonly bit and retry file deletion on Windows."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def kill_process_tree(pid: int, timeout_seconds: float = 5.0) -> None:
    """Reliably terminate a process and all of its descendants across platforms."""
    if pid <= 0:
        return

    if platform.system() == "Windows":
        try:
            # taskkill /F /T terminates the target process and any child processes started by it.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal

        try:
            pgid = os.getpgid(pid)
            if pgid == pid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@dataclass
class WorkerIsolationContext:
    """Hermetic filesystem, network, and environment state for a single parallel worker."""

    worker_id: int
    base_dir: Path
    worker_dir: Path = field(init=False)
    tmp_dir: Path = field(init=False)
    home_dir: Path = field(init=False)
    userprofile_dir: Path = field(init=False)
    database_dir: Path = field(init=False)
    git_root: Path = field(init=False)
    worktree_root: Path = field(init=False)
    git_config_path: Path = field(init=False)
    port_base: int = field(init=False)
    env: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.worker_dir = self.base_dir / f"worker_{self.worker_id:02d}"
        self.tmp_dir = self.worker_dir / "tmp"
        self.home_dir = self.worker_dir / "home"
        self.userprofile_dir = self.worker_dir / "userprofile"
        self.database_dir = self.worker_dir / "databases"
        self.git_root = self.worker_dir / "git"
        self.worktree_root = self.worker_dir / "worktrees"
        self.git_config_path = self.worker_dir / "gitconfig"
        self.port_base = 12000 + (self.worker_id * 100)

        self._setup_filesystem()
        self.env = self._build_environment()

    def _setup_filesystem(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.userprofile_dir.mkdir(parents=True, exist_ok=True)
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.git_root.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.git_config_path.write_text("# hermetic worker gitconfig\n", encoding="utf-8")

    def _build_environment(self) -> dict[str, str]:
        env = dict(os.environ)

        # 1. Hermetic temporary directory
        env["TMPDIR"] = str(self.tmp_dir)
        env["TEMP"] = str(self.tmp_dir)
        env["TMP"] = str(self.tmp_dir)

        # 2. Hermetic Git environment and AppData
        appdata = self.worker_dir / "appdata"
        localappdata = self.worker_dir / "localappdata"
        appdata.mkdir(parents=True, exist_ok=True)
        localappdata.mkdir(parents=True, exist_ok=True)

        env["HOME"] = str(self.home_dir)
        env["USERPROFILE"] = str(self.userprofile_dir)
        env["APPDATA"] = str(appdata)
        env["LOCALAPPDATA"] = str(localappdata)
        # Keep the invoking interpreter's installed packages reachable after HOME and
        # USERPROFILE move to the worker root (local Windows installs often use user-site).
        env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
        # Nested test subprocesses may intentionally replace PYTHONPATH. Pin the
        # active interpreter's user package base independently of APPDATA so they
        # retain the same installed runtime without seeing caller-selected paths.
        env["PYTHONUSERBASE"] = str(site.getuserbase())
        env["GIT_CONFIG_GLOBAL"] = str(self.git_config_path)
        env["GIT_CONFIG_SYSTEM"] = str(self.git_config_path)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_AUTHOR_NAME"] = f"StageMesh Worker {self.worker_id}"
        env["GIT_AUTHOR_EMAIL"] = f"worker-{self.worker_id}@stagemesh.test"
        env["GIT_COMMITTER_NAME"] = f"StageMesh Worker {self.worker_id}"
        env["GIT_COMMITTER_EMAIL"] = f"worker-{self.worker_id}@stagemesh.test"

        # 3. Worker metadata & port allocation
        env["STAGEMESH_TEST_WORKER_ID"] = str(self.worker_id)
        env["STAGEMESH_TEST_PORT_BASE"] = str(self.port_base)
        env["STAGEMESH_TEST_DATABASE_ROOT"] = str(self.database_dir)
        env["STAGEMESH_TEST_GIT_ROOT"] = str(self.git_root)
        env["STAGEMESH_TEST_WORKTREE_ROOT"] = str(self.worktree_root)
        env["STAGEMESH_ISOLATED_WORKER"] = "1"

        # 4. Strip ambient external provider credentials to ensure hermetic simulation
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GROK_API_KEY",
            "COHERE_API_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        ):
            env.pop(key, None)

        return env

    def cleanup(self) -> None:
        """Hermetically clean up worker temporary directory."""
        if not self.worker_dir.exists():
            return
        resolved_base = self.base_dir.resolve()
        resolved_worker = self.worker_dir.resolve()
        if resolved_worker.parent != resolved_base or not resolved_worker.name.startswith("worker_"):
            raise ValueError(f"refusing unsafe worker cleanup target: {resolved_worker}")

        for attempt in range(3):
            try:
                shutil.rmtree(self.worker_dir, onerror=_remove_readonly)
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        if self.worker_dir.exists():
            raise OSError(f"failed to clean isolated worker directory: {resolved_worker}")


class WorkerPoolIsolation:
    """Factory and manager for isolated worker contexts under a root directory."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._owned = root_dir is None
        if root_dir is None:
            self.root_dir = Path(tempfile.mkdtemp(prefix="stagemesh_workers_"))
        else:
            self.root_dir = root_dir
            self.root_dir.mkdir(parents=True, exist_ok=True)
        self._contexts: dict[int, WorkerIsolationContext] = {}

    def get_worker_context(self, worker_id: int) -> WorkerIsolationContext:
        """Obtain or initialize an isolated context for worker_id."""
        if worker_id not in self._contexts:
            ctx = WorkerIsolationContext(worker_id=worker_id, base_dir=self.root_dir)
            self._contexts[worker_id] = ctx
        return self._contexts[worker_id]

    def cleanup(self) -> None:
        """Clean up all worker contexts and root directory."""
        for ctx in self._contexts.values():
            ctx.cleanup()
        self._contexts.clear()

        if self._owned and self.root_dir.exists():
            resolved_root = self.root_dir.resolve()
            if not resolved_root.name.startswith("stagemesh_workers_"):
                raise ValueError(f"refusing unsafe worker-pool cleanup target: {resolved_root}")
            for attempt in range(3):
                try:
                    shutil.rmtree(self.root_dir, onerror=_remove_readonly)
                    return
                except OSError:
                    time.sleep(0.05 * (attempt + 1))
            if self.root_dir.exists():
                raise OSError(f"failed to clean isolated worker-pool directory: {resolved_root}")

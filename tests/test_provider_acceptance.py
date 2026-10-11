from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import subprocess
import sys
from pathlib import Path
import time

import pytest

import stagemesh.cli as cli_module
from stagemesh.capacity import CapacityKind
from stagemesh.domain import ExecutionStatus
from stagemesh.persistence import Store
from stagemesh.process_identity import process_identity
from stagemesh.provider_acceptance import (
    LiveProviderSmokeResult,
    live_provider_readiness,
    record_live_provider_acceptance,
    run_live_provider_smoke,
)
from stagemesh.providers import RuntimeCommandAdapter


def test_provider_smoke_runs_selected_provider_in_temporary_fixture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "Path('provider-smoke.txt').write_text('after\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"smoke": {"command": command, "capabilities": ["IMPLEMENT"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "provider-smoke", "--provider", "smoke", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert data["status"] == "PASS"
    assert data["provider"] == "smoke"
    assert data["execution_status"] == "SUCCEEDED"
    assert data["file_ok"] is True


def test_provider_acceptance_live_runs_selected_provider(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "Path('provider-smoke.txt').write_text('after\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"smoke": {"command": command, "capabilities": ["IMPLEMENT"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "provider-acceptance", "--live", "--provider", "smoke", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert data["status"] == "PASS"
    assert data["provider"] == "smoke"
    assert data["file_ok"] is True
    assert data["implementation_checked"] is True
    assert data["review_checked"] is False
    assert data["implementation_ready"] is True
    assert data["review_ready"] is True


def test_provider_acceptance_live_proves_review_json_for_review_provider(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "reviewer.py"
    provider.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        "print('{\"decision\":\"PASS\"}')\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"reviewer": {"command": command, "capabilities": ["REVIEW"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "provider-acceptance", "--live", "--provider", "reviewer", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert data["status"] == "PASS"
    assert data["implementation_checked"] is False
    assert data["review_checked"] is True
    assert data["implementation_ready"] is True
    assert data["review_ready"] is True
    assert data["review_json_ok"] is True


def test_provider_acceptance_live_classifies_timeout_failure_and_kills_process(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    pid_file = tmp_path / "slow.pid"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path(r'{pid_file}').write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"slow": {"command": command, "capabilities": ["IMPLEMENT"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(
            ["--project", str(project), "provider-acceptance", "--live", "--provider", "slow", "--timeout", "0.2", "--json"]
        )

    data = json.loads(out.getvalue())
    assert code == 1
    assert data["status"] == "FAIL"
    assert data["timed_out"] is True
    assert data["implementation_checked"] is True
    assert data["implementation_ready"] is False
    assert data["failure_reason"] == "provider_timeout"

    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.time() + 3.0
    killed = False
    while time.time() < deadline:
        if process_identity(pid) is None:
            killed = True
            break
        time.sleep(0.05)
    assert killed is True


def test_provider_acceptance_live_classifies_review_timeout_failure_and_kills_process(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    pid_file = tmp_path / "reviewer.pid"
    provider = tmp_path / "reviewer.py"
    provider.write_text(
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path(r'{pid_file}').write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"slow_review": {"command": command, "capabilities": ["REVIEW"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(
            ["--project", str(project), "provider-acceptance", "--live", "--provider", "slow_review", "--timeout", "0.2", "--json"]
        )

    data = json.loads(out.getvalue())
    assert code == 1
    assert data["status"] == "FAIL"
    assert data["timed_out"] is True
    assert data["review_checked"] is True
    assert data["review_ready"] is False
    assert data["failure_reason"] == "provider_timeout"

    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.time() + 3.0
    killed = False
    while time.time() < deadline:
        if process_identity(pid) is None:
            killed = True
            break
        time.sleep(0.05)
    assert killed is True


def test_run_live_provider_smoke_enforces_timeout_and_kills_hanging_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "direct.pid"
    script = tmp_path / "hanging.py"
    script.write_text(
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path(r'{pid_file}').write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    adapter = RuntimeCommandAdapter(
        name="hanging",
        command=(sys.executable, str(script)),
        capabilities=frozenset({"code"}),
    )

    result = run_live_provider_smoke(adapter, timeout_seconds=0.2)
    assert result.status == "FAIL"
    assert result.timed_out is True
    assert result.failure_reason == "provider_timeout"
    assert result.implementation_ready is False

    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.time() + 3.0
    killed = False
    while time.time() < deadline:
        if process_identity(pid) is None:
            killed = True
            break
        time.sleep(0.05)
    assert killed is True


def test_run_live_provider_smoke_classifies_adapter_timeout_exception() -> None:
    class TimeoutAdapter:
        name = "timeout"
        capabilities = frozenset({"code"})

        def check_capacity(self) -> str:
            return CapacityKind.AVAILABLE

        def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path):
            raise subprocess.TimeoutExpired(["timeout"], timeout=1)

    result = run_live_provider_smoke(TimeoutAdapter(), timeout_seconds=1)

    assert result.status == "FAIL"
    assert result.timed_out is True
    assert result.failure_reason == "provider_timeout"
    assert result.implementation_ready is False


def test_capacity_reports_command_availability_separately_from_live_readiness(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text("print('{\"decision\":\"PASS\"}')\n", encoding="utf-8")
    command = shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"smoke": {"command": command, "capabilities": ["REVIEW"]}}}),
        encoding="utf-8",
    )
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "capacity", "--json"])

    data = json.loads(out.getvalue())
    smoke = next(row for row in data["providers"] if row["provider"] == "smoke")
    assert code == 0
    assert smoke["command_available"] is True
    assert smoke["live_acceptance"] == "not_run"  # backward compatible string; per-capability readiness is separate
    assert smoke["live_readiness"]["REVIEW"]["ready"] is False
    assert "live_acceptance_unproven" in smoke["live_readiness"]["REVIEW"]["reason"]
    assert smoke["live_readiness"]["IMPLEMENT"] == "not_applicable"
    assert _review_verdict(data)["eligible"] is True  # never live-checked is still usable unless live acceptance is required


def _capacity_json(project: Path, *extra: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "capacity", "--json", *extra])
    assert code == 0
    return json.loads(out.getvalue())


def _smoke_project(tmp_path: Path, *, missing_command: bool = False) -> Path:
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text("print('{\"decision\":\"PASS\"}')" + chr(10), encoding="utf-8")
    command = "stagemesh-no-such-cli-xyz" if missing_command else shlex.join([sys.executable, str(provider)])
    (runtime / "config.json").write_text(
        json.dumps({"providers": {"smoke": {"command": command, "capabilities": ["REVIEW"]}}}),
        encoding="utf-8",
    )
    return project


def _review_verdict(data: dict, provider: str = "smoke") -> dict:
    return next(v for v in data["stages"]["REVIEW"]["verdicts"] if v["provider"] == provider)


def test_capacity_does_not_require_live_acceptance_by_default_but_can_show_the_required_view(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)

    default = _capacity_json(project)
    strict = _capacity_json(project, "--require-live-acceptance")

    default_row = next(r for r in default["providers"] if r["provider"] == "smoke")
    strict_row = next(r for r in strict["providers"] if r["provider"] == "smoke")
    assert default_row["live_acceptance"] == strict_row["live_acceptance"] == "not_run"
    assert default_row["live_readiness"] == strict_row["live_readiness"]  # readiness is reported either way
    assert "live_acceptance" not in _review_verdict(default)["reason"]  # eligible: never blocked for lack of a live check
    assert _review_verdict(default)["eligible"] is True
    required = _review_verdict(strict)
    assert required["eligible"] is False and required["reason"].startswith("live_acceptance_unproven")
    assert "provider-acceptance --live --provider smoke" in required["reason"]  # tells the operator what to run


def test_capacity_names_an_unavailable_provider_differently_from_one_that_needs_live_acceptance(tmp_path: Path) -> None:
    missing = _review_verdict(_capacity_json(_smoke_project(tmp_path / "a", missing_command=True), "--require-live-acceptance"))
    unproven = _review_verdict(_capacity_json(_smoke_project(tmp_path / "b"), "--require-live-acceptance"))

    assert missing["eligible"] is False and not missing["reason"].startswith("live_acceptance")  # unavailable: the command is the problem
    assert unproven["eligible"] is False and unproven["reason"].startswith("live_acceptance_unproven")  # available: it just was not proven


def test_live_acceptance_mode_queue_default_skips_failed_only_and_required_flag_is_opt_in() -> None:
    queue_default = argparse.Namespace()
    required = argparse.Namespace(require_live_acceptance=True)

    assert cli_module._live_acceptance_mode(queue_default, queue=True) == cli_module.LIVE_ACCEPTANCE_SKIP_FAILED
    assert cli_module._live_acceptance_mode(queue_default, queue=False) is None  # single-task continue ignores live acceptance
    assert cli_module._live_acceptance_mode(required, queue=True) == cli_module.LIVE_ACCEPTANCE_REQUIRED
    parser = cli_module.build_parser()
    assert parser.parse_args(["queue-run", "--concurrency", "1"]).require_live_acceptance is False
    assert parser.parse_args(["queue-run", "--concurrency", "1", "--require-live-acceptance"]).require_live_acceptance is True


def test_live_provider_readiness_is_capability_specific_for_mixed_results(tmp_path: Path) -> None:
    store = Store(tmp_path / "stagemesh.sqlite3")
    store.migrate()
    try:
        record_live_provider_acceptance(
            store,
            LiveProviderSmokeResult(
                "FAIL",
                "mixed",
                ExecutionStatus.FAILED,
                "candidate-sha",
                True,
                True,
                True,
                True,
                False,
                False,
                "invalid_review_json",
            ),
        )

        implementation_ready, implementation_reason = live_provider_readiness(store, "mixed", "code")
        review_ready, review_reason = live_provider_readiness(store, "mixed", "review")
    finally:
        store.close()

    assert implementation_ready is True
    assert implementation_reason == "live_acceptance_passed"
    assert review_ready is False
    assert review_reason == "live_acceptance_failed: invalid_review_json"


def test_live_acceptance_keyword_is_only_passed_to_the_coordinator_builder_when_it_is_set() -> None:
    # Regression: continue --parallel (live acceptance ignored) must call _build_coordinator exactly as before. A builder with the
    # old signature (tests and wrappers substitute it) used to fail with "unexpected keyword argument 'live_acceptance'".
    def old_signature_builder(args, project, config, store, target, log, parallel=None):
        return ("built", parallel)

    ignored = cli_module._live_acceptance_build_kwargs(cli_module._live_acceptance_mode(argparse.Namespace(), queue=False))
    queue_default = cli_module._live_acceptance_build_kwargs(cli_module._live_acceptance_mode(argparse.Namespace(), queue=True))
    strict = cli_module._live_acceptance_build_kwargs(
        cli_module._live_acceptance_mode(argparse.Namespace(require_live_acceptance=True), queue=True)
    )

    assert ignored == {}
    assert old_signature_builder(None, None, None, None, None, None, parallel="wiring", **ignored) == ("built", "wiring")
    assert queue_default == {"live_acceptance": cli_module.LIVE_ACCEPTANCE_SKIP_FAILED}
    assert strict == {"live_acceptance": cli_module.LIVE_ACCEPTANCE_REQUIRED}
    with pytest.raises(TypeError):
        old_signature_builder(None, None, None, None, None, None, parallel="wiring", **queue_default)  # only queue runs pass it

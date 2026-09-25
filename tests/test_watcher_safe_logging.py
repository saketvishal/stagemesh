"""Safe operational log redaction tests (SDD-001 section 9.7)."""

from __future__ import annotations

import json

from build_coordinator.watcher.safe_logging import WatcherLogger, redact_text


def test_redact_text_strips_github_pat():
    message = "gh command failed: token ghp_abcdefghijklmnopqrstuvwxyz0123456789 was rejected"
    redacted = redact_text(message)
    assert "ghp_" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_strips_bearer_header():
    message = "request failed: Authorization: Bearer sk-abcdefghijklmnop"
    redacted = redact_text(message)
    assert "Bearer sk-abcdefghijklmnop" not in redacted


def test_redact_text_leaves_ordinary_text_untouched():
    message = "task GH-stagemesh-orchestrator-1-T2 escalated COORDINATOR_INVARIANT_FAILURE"
    assert redact_text(message) == message


def test_watcher_logger_redacts_message_and_writes_jsonl(tmp_path):
    logger = WatcherLogger(tmp_path)
    logger.log(
        "watcher.cycle_failed",
        repository_slug="saketvishal/stagemesh-orchestrator",
        cycle_id="abc123",
        error_type="TRANSIENT_GITHUB_FAILURE",
        message="gh failed with token ghp_shouldnotappearanywhereinlog12345",
    )
    log_path = tmp_path / "watcher-logs" / "watcher.log"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ghp_" not in record["message"]
    assert record["event_type"] == "watcher.cycle_failed"
    assert record["repository_slug"] == "saketvishal/stagemesh-orchestrator"


def test_watcher_logger_extra_payload_drops_secret_like_keys(tmp_path):
    logger = WatcherLogger(tmp_path)
    logger.log(
        "watcher.cycle_succeeded",
        extra={"api_key": "should-not-be-logged", "count": 3, "nested": {"token": "also-secret"}},
    )
    log_path = tmp_path / "watcher-logs" / "watcher.log"
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert "api_key" not in record["extra"]
    assert "token" not in record["extra"].get("nested", {})
    assert record["extra"]["count"] == 3


def test_watcher_logger_never_persists_hidden_reasoning_keys(tmp_path):
    logger = WatcherLogger(tmp_path)
    logger.log("watcher.cycle_succeeded", extra={"chain_of_thought": "secret reasoning", "status": "ok"})
    log_path = tmp_path / "watcher-logs" / "watcher.log"
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert "chain_of_thought" not in record["extra"]
    assert record["extra"]["status"] == "ok"

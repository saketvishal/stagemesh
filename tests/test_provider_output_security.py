from __future__ import annotations

import json
import time
from pathlib import Path

from stagemesh.audit import export_audit_jsonl, record_audit
from stagemesh.execution import capacity_evidence
from stagemesh.persistence import Store
from stagemesh.provider_cooldown import active_cooldowns
from stagemesh.provider_pool import IMPLEMENT, ProviderPool, pool_exhaustion_evidence
from stagemesh.providers import _review_infrastructure_failure
from stagemesh.redaction import redact_mapping, sanitize_provider_output

SECRETS = (
    "private-assignment",
    "private-bearer",
    "private-prefix",
    "private-url",
)
UNTRUSTED = (
    "token=private-assignment "
    "Bearer private-bearer "
    "sk-private-prefix "
    "https://user:private-url@example.invalid/path"
)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "stagemesh.sqlite3")
    store.migrate()
    return store


def _assert_redacted(value: object) -> str:
    text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    assert all(secret not in text for secret in SECRETS)
    assert "***REDACTED***" in text
    return text


def test_provider_output_sanitizer_is_printable_bounded_and_preserves_both_ends() -> None:
    value = f"start {UNTRUSTED}\x00\x07\r\n" + "x" * 900 + "\ntail password=private-assignment"

    sanitized = sanitize_provider_output(value, 500)

    assert len(sanitized) == 500
    assert sanitized.startswith("start token=***REDACTED***")
    assert "...[truncated]..." in sanitized
    assert sanitized.endswith("tail password=***REDACTED***")
    assert "\x00" not in sanitized and "\x07" not in sanitized and "\r" not in sanitized
    _assert_redacted(sanitized)


def test_provider_output_sanitizer_never_raises_for_unprintable_objects() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("private conversion failure")

    assert sanitize_provider_output(Unprintable()) == "<unprintable provider output>"
    assert sanitize_provider_output(b"Bearer private-bearer") == "Bearer ***REDACTED***"


def test_generic_audit_redaction_scrubs_secret_text_under_non_secret_keys(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record_audit(
        store,
        "provider.custom",
        {"provider_output": UNTRUSTED, "nested": [{"review_response": UNTRUSTED}]},
    )

    payload = json.loads(store.audit_events(1)[0]["payload"])

    _assert_redacted(payload)
    assert redact_mapping({"message": UNTRUSTED})["message"].startswith(
        "token=***REDACTED***"
    )


def test_provider_failure_and_no_progress_ledgers_sanitize_before_persistence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pool = ProviderPool([], {IMPLEMENT: ()}, cooldown_seconds=60)

    pool.record_failure(
        store,
        IMPLEMENT,
        "T-1",
        "custom",
        "provider_timeout",
        provider_output=UNTRUSTED,
    )
    pool.record_no_progress(
        store,
        IMPLEMENT,
        "T-1",
        "custom",
        next_provider=None,
        sequence=["custom"],
        provider_output=UNTRUSTED,
    )

    rows = [json.loads(row["payload"]) for row in store.audit_events(10)]
    assert len(rows) == 2
    for payload in rows:
        _assert_redacted(payload["provider_output"])


def test_pool_exhaustion_and_review_failure_envelopes_never_copy_raw_output() -> None:
    evidence = pool_exhaustion_evidence(
        "all_implementation_providers_no_progress",
        [
            {
                "provider": "custom",
                "outcome": "no_implementation_change",
                "classification": "no_progress",
                "provider_output": UNTRUSTED,
            }
        ],
    )
    review = json.loads(_review_infrastructure_failure("provider_timeout", UNTRUSTED))

    _assert_redacted(evidence["provider_outcomes"][0]["provider_output"])
    _assert_redacted(review["provider_output"])


def test_capacity_evidence_uses_the_same_canonical_sanitizer() -> None:
    excerpt, retry_after = capacity_evidence(
        "startup " + UNTRUSTED,
        "retry-after: 5 minutes",
    )

    _assert_redacted(excerpt)
    assert retry_after == "retry-after: 5 minutes"


def test_audit_export_and_cooldown_projection_redact_legacy_raw_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    now = time.time()
    store.add_audit_event(
        "provider.failure",
        {
            "provider": "custom",
            "stage": IMPLEMENT,
            "task_id": "T-1",
            "reason": "quota_rate_limit",
            "retry_at": now + 60,
            "provider_output": UNTRUSTED,
        },
    )
    output = tmp_path / "audit.jsonl"

    export_audit_jsonl(store, output, root=tmp_path)
    exported = output.read_text(encoding="utf-8")
    cooldowns = active_cooldowns(store, cooldown_seconds=60, now=now)

    _assert_redacted(exported)
    assert len(cooldowns) == 1
    _assert_redacted(cooldowns[0]["last_error"])

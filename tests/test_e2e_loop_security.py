from __future__ import annotations

import json
import sys

import pytest

from stagemesh.validation_services import (
    ValidationServiceError,
    discover_validation_services,
    e2e_policy_projection,
)


def test_e2e_operator_projection_omits_targets_commands_and_raw_output() -> None:
    payload = {
        "e2e_evaluations": [
            {
                "kind": "browser",
                "result": "FAIL",
                "target": "https://private.invalid/token=secret",
                "command": ["browser", "--password=secret"],
                "stdout": "token=secret",
                "stderr": "password=secret",
                "metadata": {
                    "policy_fingerprint": "a" * 64,
                    "failure_signature": "b" * 64,
                    "evaluator": "reviewer",
                    "independent": True,
                    "requirements": [{"id": "checkout-mobile", "status": "FAIL"}],
                    "lifecycle": {
                        "managed": True,
                        "started": True,
                        "healthy": True,
                        "cleanup_confirmed": True,
                    },
                },
            }
        ]
    }

    projection = e2e_policy_projection(payload)
    encoded = json.dumps(projection)

    assert projection["status"] == "failed"
    assert projection["failure_signatures"] == ["b" * 64]
    assert projection["policies"][0]["requirements"] == [
        {"id": "checkout-mobile", "status": "FAIL"}
    ]
    assert "private.invalid" not in encoded
    assert "token=secret" not in encoded
    assert "password=secret" not in encoded
    assert "command" not in encoded


def test_e2e_operator_projection_rejects_untrusted_identity_fields() -> None:
    projection = e2e_policy_projection(
        {
            "e2e_evaluations": [
                {
                    "kind": "custom",
                    "result": "maybe",
                    "metadata": {
                        "policy_fingerprint": "not-a-digest",
                        "failure_signature": "also-not-a-digest",
                        "evaluator": "bad evaluator value",
                        "requirements": [
                            {"id": "bad requirement value", "status": "PASS"},
                            {"id": "valid-id", "status": "UNKNOWN"},
                        ],
                    },
                }
            ]
        }
    )

    assert projection == {
        "status": "pending",
        "policies": [
            {
                "kind": "unknown",
                "result": "UNKNOWN",
                "policy_fingerprint": None,
                "evaluator": None,
                "independent": False,
                "requirements": [],
                "lifecycle": {
                    "managed": False,
                    "started": False,
                    "healthy": False,
                    "cleanup_confirmed": False,
                },
                "failure_signature": None,
            }
        ],
        "failure_signatures": [],
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"startup_command": "not-an-argv", "health_command": [sys.executable, "-c", "pass"]},
        {"startup_command": [sys.executable, "-c", "pass"]},
        {
            "requirements": [
                {
                    "id": "home",
                    "kind": "browser",
                    "target": "/",
                    "state": "ready",
                    "viewport": "1280x720",
                }
            ],
            "result_manifest": ".git/config",
        },
    ],
)
def test_e2e_lifecycle_and_result_paths_fail_closed(tmp_path, extra) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    service = {
        "name": "browser",
        "kind": "browser",
        "command": [sys.executable, "-c", "pass"],
        **extra,
    }
    (runtime / "config.json").write_text(
        json.dumps({"validation_services": [service]}), encoding="utf-8"
    )

    with pytest.raises(ValidationServiceError):
        discover_validation_services(tmp_path)

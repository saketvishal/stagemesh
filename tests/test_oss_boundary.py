from __future__ import annotations

from build_coordinator.oss_boundary import (
    DOCUMENTED_EXCEPTIONS,
    evaluate_boundary,
    format_failure,
)


def test_oss_boundary_canary_fails_only_on_undocumented_coupling():
    """In the OSS package, there must be zero undocumented private coupling.

    DOCUMENTED_EXCEPTIONS is expected to be empty: all private-IP has been
    extracted. If this test fails, a new private-IP coupling was introduced.
    """
    allowed, unexpected = evaluate_boundary()
    assert unexpected == [], format_failure(unexpected)
    # In the OSS package, DOCUMENTED_EXCEPTIONS must be empty.
    assert len(DOCUMENTED_EXCEPTIONS) == 0, (
        f"OSS package has {len(DOCUMENTED_EXCEPTIONS)} documented exception(s) still pending extraction: "
        + str(DOCUMENTED_EXCEPTIONS)
    )


def test_oss_boundary_failure_output_is_actionable():
    from build_coordinator.oss_boundary import BoundaryFinding

    text = format_failure(
        [BoundaryFinding("new_module.py", "legacy_caventra_env", "CAVENTRA_* name 'CAVENTRA_SECRET'", 12)]
    )
    assert "new_module.py:12" in text
    assert "legacy_caventra_env" in text or "CAVENTRA_SECRET" in text
    assert "DOCUMENTED_EXCEPTIONS" in text

"""Guards that docs/operator-ui-contract.md stays in step with the activity projection it specifies."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stagemesh import activity
from stagemesh.activity import ActivityEntry, ActivityQuery, ActivityQueryError, validate_query
from stagemesh.domain import Stage

DOC = (Path(__file__).resolve().parents[1] / "docs" / "operator-ui-contract.md").read_text(encoding="utf-8")


def test_documents_every_projection_field_and_bound():
    for field in dataclasses.fields(ActivityEntry):
        assert f"`{field.name}`" in DOC, field.name
    for field in dataclasses.fields(ActivityQuery):
        assert f"`{field.name}`" in DOC, field.name
    for page_field in ("total", "limit", "offset", "next_offset", "truncated"):
        assert f'"{page_field}"' in DOC
    assert f"1 to {activity.MAX_LIMIT}" in DOC
    assert f"default {activity.DEFAULT_LIMIT}" in DOC
    assert f"0 to {activity.MAX_SCAN - 1}" in DOC
    assert str(activity.MAX_SCAN) in DOC


def test_documents_the_complete_outcome_allowlist_and_stages():
    for code in activity._SAFE_RESULTS:
        assert f"`{code}`" in DOC, code
    assert "`Stage` value" in DOC
    for stage in Stage:
        assert f"`{stage.value}`" in DOC, stage.value
    for kind in ("task", "execution", "audit"):
        assert f"`{kind}`" in DOC


def test_read_only_and_fail_closed_posture_is_stated():
    for phrase in ("`GET` only", "fail closed", "loopback", "Bearer", "DNS", "`invalid_query`", "`store_unavailable`"):
        assert phrase in DOC, phrase
    assert "## Decisions needed before implementing #135" in DOC
    assert "## Known gaps against #134" in DOC


@pytest.mark.parametrize(
    "query",
    [
        ActivityQuery(state="pending"),
        ActivityQuery(stage="NOPE"),
        ActivityQuery(task_id=""),
        ActivityQuery(provider="x" * 201),
        ActivityQuery(since=datetime(2026, 1, 1)),  # noqa: DTZ001 - naive on purpose
        ActivityQuery(since=datetime(2026, 1, 2, tzinfo=UTC), until=datetime(2026, 1, 1, tzinfo=UTC)),
        ActivityQuery(limit=0),
        ActivityQuery(limit=activity.MAX_LIMIT + 1),
        ActivityQuery(offset=-1),
        ActivityQuery(offset=activity.MAX_SCAN),
    ],
)
def test_documented_invalid_queries_fail_closed(query):
    with pytest.raises(ActivityQueryError):
        validate_query(query)


def test_documented_valid_boundaries_pass():
    validate_query(ActivityQuery(limit=activity.MAX_LIMIT, offset=activity.MAX_SCAN - 1))
    validate_query(ActivityQuery(state="open", stage="IMPLEMENT", since=datetime(2026, 1, 1, tzinfo=UTC)))

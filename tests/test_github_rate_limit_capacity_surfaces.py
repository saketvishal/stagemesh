"""Regression tests for Problem 3 (GH-133): GitHub rate-limit handling must
cover outbound comments/label/state sync in addition to discovery, must
suppress *actual* repeated GitHub requests during a known cooldown (not
just repeated warnings), and a rate limit on one surface (label
provisioning) must never gate a different surface (discovery, outbound
sync) -- label provisioning is explicitly not a prerequisite for reading
or executing work. Authentication/permission failures must remain a
distinct, actionable diagnostic rather than being folded into capacity
handling.

Tasks are set up via `discover_tasks()` against a client double (matching
tests/test_github_outbound_sync.py's MockGitHubClient pattern) so the task
carries real GitHub source identity metadata -- a bare BuildTask row with
no source metadata never resolves an issue number and is not a fixture
these tests should depend on.
"""
from __future__ import annotations

from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.task_source.github import GitHubTaskSource


class _HeaderedError(RuntimeError):
    """A client-side error that (like a real HTTP client's exception) can
    carry raw response headers, so a test can DEMONSTRATE a narrow-scope
    capacity hit rather than relying on message wording alone -- see
    GitHubTaskSource._is_primary_capacity_message /
    _extract_capacity_headers."""

    def __init__(self, message: str, *, headers: dict | None = None) -> None:
        super().__init__(message)
        self.headers = headers


class RateLimitableClient:
    """Minimal GitHub client double whose calls can be individually forced
    to raise a rate-limit or a non-rate-limit (auth) error on demand, with
    an actual call counter per method so tests can directly verify a real
    request was (or was not) suppressed -- not just infer it from a
    recorded event."""

    def __init__(self, issues):
        self.issues = list(issues)
        self.comments: list[dict] = []
        self.closed: list[str] = []
        self.labels: list[dict] = []
        self.fail_calls: dict[str, str] = {}  # call name -> error message
        self.fail_headers: dict[str, dict] = {}  # call name -> headers to attach to that failure
        self.call_counts: dict[str, int] = {}

    def _maybe_fail(self, call: str) -> None:
        self.call_counts[call] = self.call_counts.get(call, 0) + 1
        if call in self.fail_calls:
            raise _HeaderedError(self.fail_calls[call], headers=self.fail_headers.get(call))

    def list_issues(self, repo, labels):
        self._maybe_fail("list_issues")
        return self.issues

    def list_labels(self, repo):
        self._maybe_fail("list_labels")
        return []

    def create_label(self, repo, name):
        self._maybe_fail("create_label")

    def add_comment(self, repo, number, body):
        self._maybe_fail("add_comment")
        self.comments.append({"repo": repo, "number": str(number), "body": body})

    def add_label(self, repo, number, label):
        self._maybe_fail("add_label")
        self.labels.append({"repo": repo, "number": str(number), "label": label})

    def remove_label(self, repo, number, label):
        self._maybe_fail("remove_label")

    def close_issue(self, repo, number):
        self._maybe_fail("close_issue")
        self.closed.append(str(number))


def _discover_one_task(source: GitHubTaskSource) -> str:
    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()
    assert len(results) == 1
    return results[0].task_id


def _capacity_wait_events(session, surface: str | None = None) -> list[BuildTaskEvent]:
    events = session.scalars(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.event_type == "task_source.capacity_wait")
        .where(BuildTaskEvent.task_id.is_(None))
    ).all()
    if surface is None:
        return events
    return [e for e in events if (e.event_data or {}).get("surface") == surface]


def _issue(number: int) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Body\n\n### Acceptance Criteria\n- done",
        "labels": [{"name": "review:independent"}],
        "url": f"https://github.com/example/repo/issues/{number}",
    }


def setup_module(_module) -> None:
    Base.metadata.drop_all(bind=engine)
    initialize_schema()


def setup_function(_fn) -> None:
    Base.metadata.drop_all(bind=engine)
    initialize_schema()


def test_outbound_rate_limit_suppresses_repeated_client_calls_until_cooldown_clears():
    """A rate-limited 'add_comment' call must be classified as a typed
    capacity wait on the outbound surface, and a second sync_outbound
    attempt within the same cooldown window must not re-invoke the client
    call at all -- it must be deferred with a distinct
    'task_source_capacity_wait' diagnostic instead of hammering GitHub
    again."""
    client = RateLimitableClient([_issue(201)])
    source = GitHubTaskSource(repo="example/repo", client=client)
    task_id = _discover_one_task(source)

    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        session.commit()

    client.fail_calls["add_comment"] = "API rate limit exceeded for installation"

    with SessionLocal() as session:
        ok = source.sync_outbound(session, task_id, "DONE")
        assert ok is False
        session.commit()
        assert len(_capacity_wait_events(session, surface="outbound")) == 1

    assert len(client.comments) == 0  # the attempted comment was never actually recorded (it raised)

    # Second attempt within the cooldown window: client.add_comment must not
    # even be called, and the client's fail_calls path can never fire again
    # because we short-circuit before invoking the client at all -- verified
    # by clearing the forced failure and confirming *nothing* got through.
    client.fail_calls.pop("add_comment")
    with SessionLocal() as session:
        ok_again = source.sync_outbound(session, task_id, "DONE")
        assert ok_again is False
        session.commit()
        failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "task.outbound_sync_failed")
        ).all()
        assert failures[-1].event_data.get("action") == "task_source_capacity_wait"

    # Comment was still never actually posted -- the deferred attempt made
    # no client call, even though add_comment would have succeeded now.
    assert len(client.comments) == 0


def test_undemonstrated_label_provisioning_rate_limit_is_treated_as_shared():
    """A rate limit hit while provisioning lifecycle labels, with NO header
    evidence pinning its scope, must be treated as an unknown-scope
    restriction -- and an unknown-scope restriction defaults to shared, not
    narrow (see GitHubTaskSource._is_primary_capacity_message). Message
    wording alone ("secondary rate limit exceeded...") is never sufficient
    to prove narrow scope: it describes what the server called the
    restriction, not which surfaces it actually affects. So discovery must
    genuinely be deferred by the same cooldown -- not "attempt a live
    fetch regardless" (the previous, wording-trusting assumption this test
    used to encode). See test_demonstrated_narrow_label_limit_does_not_gate_
    other_surfaces below for the header-DEMONSTRATED case, where narrow
    scope is legitimately honored."""
    client = RateLimitableClient([_issue(202)])
    client.fail_calls["list_labels"] = "secondary rate limit exceeded while listing labels"
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()
        # Discovery itself still ran this first time (labels failing
        # doesn't block reading work) -- the *shared* cooldown is only
        # recorded as a side effect of the labels failure, not yet
        # consulted by discovery's own upcoming attempt.
        assert len(results) == 1
        assert len(_capacity_wait_events(session, surface="labels")) == 1
        assert source._capacity_cooldown_active(session, surface="labels") is True
        # No header evidence was provided -- unknown scope -- so the shared
        # cross-surface gate is genuinely active too.
        assert source._capacity_cooldown_active(session, surface="discovery") is True
        assert source._capacity_cooldown_active(session, surface="outbound") is True

    task_id = results[0].task_id
    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        session.commit()

    # A second discover_tasks() call must not make a real list_issues call
    # at all while the shared cooldown from the unverified "secondary"
    # message is active.
    calls_before = client.call_counts.get("list_issues", 0)
    client.fail_calls.pop("list_labels", None)
    with SessionLocal() as session:
        results2 = source.discover_tasks(session)
        session.commit()
    assert client.call_counts.get("list_issues", 0) == calls_before
    assert len(results2) == 1
    assert results2[0].action == "SOURCE_CAPACITY_WAIT"


def test_demonstrated_narrow_label_limit_does_not_gate_other_surfaces():
    """The DEMONSTRATED counterpart to the test above: when the label
    -provisioning failure carries real header evidence that the shared
    primary quota is NOT exhausted (X-RateLimit-Remaining present and
    non-zero), the restriction is narrow/independent by proof, not by
    trusting its wording -- and only then must discovery and outbound sync
    keep running unaffected, exactly as "preserve legitimately independent
    fallback behavior" requires."""
    client = RateLimitableClient([_issue(2020)])
    client.fail_calls["list_labels"] = "secondary rate limit exceeded while listing labels"
    client.fail_headers["list_labels"] = {"X-RateLimit-Remaining": "4999"}
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()
        assert len(results) == 1
        assert len(_capacity_wait_events(session, surface="labels")) == 1
        assert source._capacity_cooldown_active(session, surface="labels") is True
        # Demonstrated narrow: the shared surfaces are NOT gated.
        assert source._capacity_cooldown_active(session, surface="discovery") is False
        assert source._capacity_cooldown_active(session, surface="outbound") is False

    task_id = results[0].task_id
    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        session.commit()

    # Discovery genuinely still attempts a real list_issues call.
    calls_before = client.call_counts.get("list_issues", 0)
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
    assert client.call_counts.get("list_issues", 0) == calls_before + 1

    # Labels are still cooling down on their own surface, so outbound sync's
    # pre-existing label-provisioning gate still applies -- but that must be
    # reported as "label_provisioning", never folded into a discovery/
    # outbound capacity wait.
    with SessionLocal() as session:
        ok = source.sync_outbound(session, task_id, "DONE")
        session.commit()
        assert ok is False
        failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "task.outbound_sync_failed")
        ).all()
        assert failures[-1].event_data.get("action") == "label_provisioning"
        assert len(_capacity_wait_events(session, surface="outbound")) == 0


def test_auth_failure_in_outbound_sync_is_not_classified_as_capacity_wait():
    """A non-rate-limit failure (bad credentials) must remain a distinct,
    actionable diagnostic and must never be recorded as a source-capacity
    wait or suppress subsequent attempts."""
    client = RateLimitableClient([_issue(203)])
    source = GitHubTaskSource(repo="example/repo", client=client)
    task_id = _discover_one_task(source)

    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        session.commit()

    client.fail_calls["add_comment"] = "HTTP 401: Bad credentials"

    with SessionLocal() as session:
        ok = source.sync_outbound(session, task_id, "DONE")
        assert ok is False
        session.commit()
        assert len(_capacity_wait_events(session)) == 0
        failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "task.outbound_sync_failed")
        ).all()
        assert failures[-1].event_data.get("action") == "client_sync"
        assert "401" in failures[-1].event_data.get("error", "")
        assert source._capacity_cooldown_active(session, surface="outbound") is False


def test_primary_capacity_message_gates_other_rest_surfaces_via_shared_backoff():
    """A *primary*, account-wide REST quota message (no 'secondary rate' or
    'abuse detection' phrasing) hit while provisioning labels must gate
    every other REST-backed surface (discovery, outbound) through the
    shared backoff, on top of its own "labels" cooldown -- unlike a
    genuinely secondary/abuse-detection limit (see
    test_label_provisioning_rate_limit_does_not_block_discovery_or_outbound),
    which stays scoped to its own surface alone."""
    client = RateLimitableClient([_issue(210)])
    client.fail_calls["list_labels"] = "API rate limit exceeded for installation"
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
        assert len(_capacity_wait_events(session, surface="labels")) == 1
        # The shared, account-wide quota is now exhausted: every other
        # REST-backed surface must honor the same cooldown, even though
        # nothing failed on those surfaces directly.
        assert source._capacity_cooldown_active(session, surface="discovery") is True
        assert source._capacity_cooldown_active(session, surface="outbound") is True
        assert source._capacity_cooldown_active(session, surface="labels") is True

    # Discovery genuinely honors the shared gate: a second discover_tasks()
    # call must not re-invoke list_issues at all while the shared primary
    # cooldown is active, even though list_issues itself never failed.
    calls_before = client.fail_calls.copy()
    client.fail_calls.clear()  # list_issues would now succeed if actually called
    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()
    # Deferred by the shared cooldown -- not a real (and therefore
    # cache-refreshing) discovery attempt, so it is reported as a capacity
    # wait rather than as a genuinely empty queue.
    assert len(results) == 1
    assert results[0].action == "SOURCE_CAPACITY_WAIT"
    client.fail_calls.update(calls_before)


def test_unverified_secondary_wording_alone_is_not_treated_as_narrow():
    """Direct regression for the corrected classifier: message wording
    ("secondary rate limit", "abuse detection") must never, by itself,
    demonstrate narrow scope. With no header evidence at all, this is
    identical in effect to a primary hit -- the shared surface is gated
    too. (Contrast test_demonstrated_narrow_label_limit_does_not_gate_
    other_surfaces, which supplies real header evidence and correctly
    stays narrow.)"""
    client = RateLimitableClient([_issue(211)])
    client.fail_calls["list_labels"] = "secondary rate limit exceeded while listing labels"
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
        assert len(_capacity_wait_events(session, surface="labels")) == 1
        assert source._capacity_cooldown_active(session, surface="labels") is True
        assert source._capacity_cooldown_active(session, surface="discovery") is True
        assert source._capacity_cooldown_active(session, surface="outbound") is True


def test_actual_client_request_count_is_suppressed_during_cooldown():
    """Directly counts real client-side calls (not just recorded events) to
    verify the cooldown suppresses the actual outbound GitHub request, not
    merely the warning/log line, across repeated attempts within the
    cooldown window, and resumes making real requests once it genuinely
    elapses."""
    client = RateLimitableClient([_issue(212)])
    call_count = {"add_comment": 0}
    real_add_comment = client.add_comment

    def counting_add_comment(repo, number, body):
        call_count["add_comment"] += 1
        return real_add_comment(repo, number, body)

    client.add_comment = counting_add_comment
    source = GitHubTaskSource(repo="example/repo", client=client)
    task_id = _discover_one_task(source)

    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        session.commit()

    client.fail_calls["add_comment"] = "API rate limit exceeded for installation"
    with SessionLocal() as session:
        assert source.sync_outbound(session, task_id, "DONE") is False
        session.commit()
    assert call_count["add_comment"] == 1

    # Three more attempts land within the still-active cooldown: none of
    # them may reach the client's add_comment at all.
    for _ in range(3):
        with SessionLocal() as session:
            assert source.sync_outbound(session, task_id, "DONE") is False
            session.commit()
    assert call_count["add_comment"] == 1

    # Once the cooldown has genuinely elapsed, the next attempt makes a
    # real request again. The recorded failure message ("API rate limit
    # exceeded for installation") is classified as *primary*, so it also
    # set the shared cross-surface backoff -- that must be cleared too, not
    # just "outbound"'s own entry, or the shared gate would still suppress
    # this attempt.
    client.fail_calls.pop("add_comment")
    with SessionLocal() as session:
        source._capacity_backoff_until_by_surface["outbound"] = 0.0
        source._capacity_backoff_until_by_surface[source._SHARED_PRIMARY_SURFACE] = 0.0
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "task_source.capacity_wait")
        ).all()
        for event in events:
            data = dict(event.event_data or {})
            if data.get("surface") == "outbound" or data.get("primary"):
                data["backoff_until"] = 0.0
                event.event_data = data
                session.add(event)
        session.commit()

        assert source.sync_outbound(session, task_id, "DONE") is True
        session.commit()
    assert call_count["add_comment"] == 2


def test_undemonstrated_shared_cooldown_survives_source_restart_and_recovers():
    """The shared-scope default from an unverified 'secondary' message must
    itself be durable across a source-instance restart (a fresh
    GitHubTaskSource with an empty in-memory backoff dict, matching what a
    real executor/process restart looks like) -- not just an in-memory
    flag -- and must genuinely recover (stop suppressing) once the
    persisted backoff has actually elapsed, on every surface it gated."""
    client = RateLimitableClient([_issue(212)])
    client.fail_calls["list_labels"] = "secondary rate limit exceeded while listing labels"
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
        assert source._capacity_cooldown_active(session, surface="discovery") is True

    # Simulate a restart: a brand-new source instance, empty in-memory
    # dict, same repo/client.
    restarted = GitHubTaskSource(repo="example/repo", client=client)
    client.fail_calls.pop("list_labels", None)  # would succeed for real now
    calls_before = client.call_counts.get("list_issues", 0)
    with SessionLocal() as session:
        results = restarted.discover_tasks(session)
        session.commit()
    # The restarted instance still honors the persisted shared cooldown: no
    # real list_issues call, deferred result.
    assert client.call_counts.get("list_issues", 0) == calls_before
    assert len(results) == 1
    assert results[0].action == "SOURCE_CAPACITY_WAIT"

    # Force genuine elapse (both the "labels" and shared entries) and
    # confirm recovery on the restarted instance.
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "task_source.capacity_wait")
        ).all()
        for event in events:
            data = dict(event.event_data or {})
            data["backoff_until"] = 0.0
            event.event_data = data
            session.add(event)
        session.commit()
    restarted._capacity_backoff_until_by_surface.clear()

    with SessionLocal() as session:
        results3 = restarted.discover_tasks(session)
        session.commit()
    assert client.call_counts.get("list_issues", 0) == calls_before + 1
    assert len(results3) == 1
    assert results3[0].action != "SOURCE_CAPACITY_WAIT"

"""Transient failure classification and backoff (SDD-001 section 4.6).

Watcher-cycle failures are classified so that infrastructure hiccups never
get treated as task implementation failures:

- POLICY_FAILURE: authorization, invalid config, protected-branch gate,
  malformed result contract, coordinator invariant failure. No retry loop.
- TRANSIENT_GITHUB_FAILURE: rate limit, 5xx, secondary rate limit, timeout,
  DNS, TLS, connection reset from a GitHub API/CLI call. Retry with backoff.
- TRANSIENT_NETWORK_FAILURE: non-GitHub network errors (git fetch/push/
  ls-remote, label provisioning transport failures). Retry with backoff.
- WORKER_FAILURE: a worker process failure -- handled by existing runner
  execution reconciliation, not by the watcher loop's own backoff.
- UNKNOWN_FAILURE: fail closed after logging redacted detail; leave
  existing task state unchanged unless existing runner policy already
  transitions it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from build_coordinator.github.client import GitHubClientError
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.watcher.authorization import RepositoryAuthorizationError

FAILURE_CLASSES = (
    "POLICY_FAILURE",
    "TRANSIENT_GITHUB_FAILURE",
    "TRANSIENT_NETWORK_FAILURE",
    "WORKER_FAILURE",
    "UNKNOWN_FAILURE",
)

_GITHUB_TRANSIENT_MARKERS = (
    "rate limit",
    "secondary rate limit",
    "502",
    "503",
    "504",
    "500",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "could not resolve host",
    "connection reset",
    "tls",
    "ssl",
)
_NETWORK_TRANSIENT_MARKERS = (
    "could not resolve host",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "timed out",
    "timeout",
    "temporary failure in name resolution",
    "unable to access",
)


def classify_failure(exc: BaseException) -> str:
    """Classify an exception raised during a watcher cycle. Fails closed:
    anything not positively identified as transient is a policy failure or
    unknown failure, never silently treated as retryable."""
    if isinstance(exc, RepositoryAuthorizationError):
        return "POLICY_FAILURE"
    message = str(exc).lower()
    # GitHubClientError subclasses CoordinatorPolicyError, so it must be
    # checked before the generic CoordinatorPolicyError branch below --
    # otherwise a transient GitHub 5xx/rate-limit error would always be
    # misclassified as a non-retryable policy failure.
    if isinstance(exc, GitHubClientError):
        if any(marker in message for marker in _GITHUB_TRANSIENT_MARKERS):
            return "TRANSIENT_GITHUB_FAILURE"
        return "POLICY_FAILURE"
    if isinstance(exc, CoordinatorPolicyError):
        return "POLICY_FAILURE"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        if any(marker in message for marker in _NETWORK_TRANSIENT_MARKERS) or isinstance(
            exc, (ConnectionError, TimeoutError)
        ):
            return "TRANSIENT_NETWORK_FAILURE"
    if any(marker in message for marker in _NETWORK_TRANSIENT_MARKERS):
        return "TRANSIENT_NETWORK_FAILURE"
    return "UNKNOWN_FAILURE"


def is_retryable(failure_class: str) -> bool:
    return failure_class in ("TRANSIENT_GITHUB_FAILURE", "TRANSIENT_NETWORK_FAILURE")


@dataclass(frozen=True)
class BackoffPolicy:
    base_seconds: float = 5.0
    max_seconds: float = 300.0
    multiplier: float = 2.0
    jitter_fraction: float = 0.2

    def delay_seconds(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """`attempt` is 1 for the first retry after an initial failure.
        Exponential base delay capped at `max_seconds`, plus symmetric
        jitter so repeated backoffs across many watcher restarts don't
        synchronize into a thundering herd against GitHub."""
        if attempt < 1:
            attempt = 1
        raw = min(self.base_seconds * (self.multiplier ** (attempt - 1)), self.max_seconds)
        jitter_span = raw * self.jitter_fraction
        rng = rng or random.Random()
        jitter = rng.uniform(-jitter_span, jitter_span)
        return max(0.0, raw + jitter)


DEFAULT_BACKOFF = BackoffPolicy()

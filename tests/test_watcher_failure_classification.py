"""Transient failure classification and backoff tests (SDD-001 section 9.6)."""

from __future__ import annotations

import random

from build_coordinator.github.client import GitHubClientError
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.watcher.authorization import RepositoryAuthorizationError
from build_coordinator.watcher.failure_classification import (
    BackoffPolicy,
    classify_failure,
    is_retryable,
)


def test_repository_authorization_error_is_policy_failure():
    assert classify_failure(RepositoryAuthorizationError("bad config")) == "POLICY_FAILURE"


def test_coordinator_policy_error_is_policy_failure():
    assert classify_failure(CoordinatorPolicyError("invariant failure")) == "POLICY_FAILURE"


def test_github_rate_limit_is_transient_github_failure():
    exc = GitHubClientError("gh command failed (code 1): API rate limit exceeded")
    assert classify_failure(exc) == "TRANSIENT_GITHUB_FAILURE"


def test_github_5xx_is_transient_github_failure():
    exc = GitHubClientError("gh command failed (code 1): HTTP 503 Service Unavailable")
    assert classify_failure(exc) == "TRANSIENT_GITHUB_FAILURE"


def test_github_auth_failure_is_policy_failure_not_transient():
    exc = GitHubClientError("gh command failed (code 4): authentication required")
    assert classify_failure(exc) == "POLICY_FAILURE"


def test_connection_error_is_transient_network_failure():
    assert classify_failure(ConnectionError("connection reset by peer")) == "TRANSIENT_NETWORK_FAILURE"


def test_timeout_error_is_transient_network_failure():
    assert classify_failure(TimeoutError("timed out")) == "TRANSIENT_NETWORK_FAILURE"


def test_unrecognized_error_is_unknown_failure():
    assert classify_failure(ValueError("something unexpected")) == "UNKNOWN_FAILURE"


def test_is_retryable_only_for_transient_classes():
    assert is_retryable("TRANSIENT_GITHUB_FAILURE")
    assert is_retryable("TRANSIENT_NETWORK_FAILURE")
    assert not is_retryable("POLICY_FAILURE")
    assert not is_retryable("WORKER_FAILURE")
    assert not is_retryable("UNKNOWN_FAILURE")


def test_backoff_is_exponential_and_capped():
    policy = BackoffPolicy(base_seconds=5.0, max_seconds=60.0, multiplier=2.0, jitter_fraction=0.0)
    rng = random.Random(0)
    assert policy.delay_seconds(1, rng=rng) == 5.0
    assert policy.delay_seconds(2, rng=rng) == 10.0
    assert policy.delay_seconds(3, rng=rng) == 20.0
    assert policy.delay_seconds(10, rng=rng) == 60.0  # capped


def test_backoff_jitter_stays_within_bounds():
    policy = BackoffPolicy(base_seconds=10.0, max_seconds=100.0, multiplier=2.0, jitter_fraction=0.2)
    rng = random.Random(1)
    delay = policy.delay_seconds(1, rng=rng)
    assert 8.0 <= delay <= 12.0

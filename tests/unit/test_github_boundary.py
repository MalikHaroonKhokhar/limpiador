"""Unit tests for the GitHub external-call boundary (ARCHITECTURE.md §5.3, §13).

The boundary is the single chokepoint every ``github.*`` tool routes its API
calls through, so retry/backoff and rate limiting live in one place rather than
scattered across the fourteen tools. These tests drive it with fake operations
that raise PyGitHub's own exception types — no network — and assert that
transient failures (rate limits, 5xx, dropped connections) are retried, that a
non-transient failure (a 404) is not, and that calls are metered by the limiter.
Backoff and throttle run on an injected fake clock, so there is no real waiting.
"""

from __future__ import annotations

import pytest
import requests
from github import GithubException, RateLimitExceededException

from limpiador.observability.errors import TransientError
from limpiador.observability.retry import RateLimit, Resilience, RetryPolicy
from limpiador.tools.github_client import GitHubBoundary, is_transient_status


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _boundary(clock: _FakeClock, *, max_attempts: int = 4, rate: float = 1000.0, burst: int = 1000) -> GitHubBoundary:
    return GitHubBoundary(
        resilience=Resilience(
            retry=RetryPolicy(max_attempts=max_attempts, base_delay_s=0.01),
            rate_limit=RateLimit(rate_per_second=rate, burst=burst),
            sleep=clock.sleep,
            clock=clock.time,
        )
    )


def _flaky(errors: list[Exception], result: str = "ok"):
    state = {"calls": 0}

    def operation() -> str:
        state["calls"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return operation, state


def test_transient_status_predicate() -> None:
    assert is_transient_status(429) and is_transient_status(503)
    assert not is_transient_status(404) and not is_transient_status(200)


def test_a_transient_server_error_is_retried_then_succeeds() -> None:
    clock = _FakeClock()
    operation, state = _flaky([GithubException(503, {}, None), GithubException(502, {}, None)])

    assert _boundary(clock).call(operation) == "ok"
    assert state["calls"] == 3  # two 5xx failures, then success


def test_a_rate_limit_response_is_retried() -> None:
    clock = _FakeClock()
    operation, state = _flaky([RateLimitExceededException(403, {"message": "rate limit"}, None)])

    assert _boundary(clock).call(operation) == "ok"
    assert state["calls"] == 2  # the rate limit was treated as transient


def test_a_dropped_connection_is_retried() -> None:
    clock = _FakeClock()
    operation, state = _flaky([requests.ConnectionError("connection reset")])

    assert _boundary(clock).call(operation) == "ok"
    assert state["calls"] == 2


def test_persistent_transient_failures_give_up_with_a_typed_error() -> None:
    clock = _FakeClock()
    operation, state = _flaky([GithubException(503, {}, None) for _ in range(3)])

    with pytest.raises(TransientError):
        _boundary(clock, max_attempts=3).call(operation)
    assert state["calls"] == 3  # exactly max_attempts, then a typed give-up


def test_a_non_transient_github_error_is_not_retried() -> None:
    clock = _FakeClock()
    operation, state = _flaky([GithubException(404, {"message": "not found"}, None)])

    with pytest.raises(GithubException):
        _boundary(clock, max_attempts=5).call(operation)
    assert state["calls"] == 1  # a 404 is the agent's to read, not a retry's to fix


def test_the_boundary_meters_calls_through_its_limiter() -> None:
    clock = _FakeClock()
    boundary = _boundary(clock, rate=10.0, burst=1)

    boundary.call(lambda: "a")  # the single burst token — immediate
    boundary.call(lambda: "b")  # throttled to 10/s → 0.1s

    assert abs(clock.now - 0.1) < 1e-9

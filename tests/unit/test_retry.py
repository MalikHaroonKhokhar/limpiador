"""Unit tests for bounded retry with exponential backoff (ARCHITECTURE.md §13).

Real external calls (GitHub, the model) fail transiently. The retry util retries
a transient failure a bounded number of times with exponential backoff, then
gives up with a *typed* exhausted ``TransientError`` — a retry that never gives
up is just a slower infinite loop. A non-transient failure is never retried.

Backoff sleeps are injected (``sleep=list.append``), so the tests assert the
exact backoff schedule deterministically, with no real waiting.
"""

from __future__ import annotations

import pytest

from limpiador.observability.errors import NotFoundError, TransientError
from limpiador.observability.retry import RetryPolicy, retrying


def test_succeeds_on_the_first_try_without_sleeping() -> None:
    sleeps: list[float] = []
    assert retrying(lambda: "value", policy=RetryPolicy(), sleep=sleeps.append) == "value"
    assert sleeps == []


def test_retries_a_transient_failure_then_succeeds() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientError("blip")
        return "ok"

    result = retrying(
        flaky, policy=RetryPolicy(max_attempts=5, base_delay_s=0.1), sleep=sleeps.append
    )

    assert result == "ok"
    assert attempts["n"] == 3  # failed twice, succeeded on the third
    assert len(sleeps) == 2  # slept before the two retries, not after success


def test_exhausts_retries_and_raises_a_typed_give_up() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def always_transient() -> str:
        attempts["n"] += 1
        raise TransientError("still down")

    with pytest.raises(TransientError) as caught:
        retrying(
            always_transient,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.1),
            sleep=sleeps.append,
        )

    assert attempts["n"] == 3  # tried exactly max_attempts times
    assert "3" in str(caught.value)  # the give-up names how many attempts it made
    assert caught.value.__cause__ is not None  # chains the last transient failure
    assert len(sleeps) == 2  # slept between the three attempts, never after the last


def test_a_non_transient_failure_is_not_retried() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def hard_failure() -> str:
        attempts["n"] += 1
        raise NotFoundError("no such file")

    with pytest.raises(NotFoundError):
        retrying(hard_failure, policy=RetryPolicy(max_attempts=5), sleep=sleeps.append)

    assert attempts["n"] == 1  # raised straight through, no retry
    assert sleeps == []


def test_backoff_grows_exponentially() -> None:
    sleeps: list[float] = []

    def down() -> str:
        raise TransientError("x")

    with pytest.raises(TransientError):
        retrying(
            down,
            policy=RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=100.0),
            sleep=sleeps.append,
        )

    assert sleeps == [0.5, 1.0, 2.0]  # before attempts 2, 3, 4


def test_backoff_is_capped_at_the_max_delay() -> None:
    sleeps: list[float] = []

    def down() -> str:
        raise TransientError("x")

    with pytest.raises(TransientError):
        retrying(
            down,
            policy=RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=2.0),
            sleep=sleeps.append,
        )

    assert sleeps == [1.0, 2.0, 2.0, 2.0]  # 1, then capped at 2

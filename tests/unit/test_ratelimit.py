"""Unit tests for the token-bucket rate limiter (ARCHITECTURE.md §13).

A token bucket caps the rate of external calls so limpiador is not throttled or
banned during a busy run: it allows a short ``burst`` immediately, then meters
further calls to ``rate_per_second``. The clock and sleep are injected via a fake
clock, so throttling is asserted deterministically with no real time passing —
``sleep`` only advances the fake clock.
"""

from __future__ import annotations

from limpiador.observability.retry import RateLimit, TokenBucket


class _FakeClock:
    """A deterministic clock: ``sleep`` advances ``now`` instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _bucket(rate: float, burst: int) -> tuple[TokenBucket, _FakeClock]:
    clock = _FakeClock()
    bucket = TokenBucket(RateLimit(rate_per_second=rate, burst=burst), clock=clock.time, sleep=clock.sleep)
    return bucket, clock


def test_an_initial_burst_is_allowed_immediately() -> None:
    bucket, clock = _bucket(rate=10.0, burst=3)
    for _ in range(3):
        bucket.acquire()
    assert clock.now == 0.0  # the full burst cost no time


def test_calls_past_the_burst_are_metered_to_the_rate() -> None:
    bucket, clock = _bucket(rate=10.0, burst=3)
    for _ in range(3):  # drain the burst
        bucket.acquire()

    for _ in range(5):  # five more, throttled to 10/s → 0.1s each
        bucket.acquire()

    assert abs(clock.now - 0.5) < 1e-9


def test_tokens_refill_over_time_and_allow_a_later_burst() -> None:
    bucket, clock = _bucket(rate=5.0, burst=2)
    bucket.acquire()
    bucket.acquire()  # bucket drained at t=0
    assert clock.now == 0.0

    clock.now += 1.0  # a second passes between calls → refills (capped at burst=2)
    bucket.acquire()
    bucket.acquire()  # two refilled tokens spend no extra time
    assert clock.now == 1.0

    bucket.acquire()  # the third must wait 1/5s for a fresh token
    assert abs(clock.now - 1.2) < 1e-9


def test_refill_never_exceeds_the_burst_capacity() -> None:
    bucket, clock = _bucket(rate=5.0, burst=2)
    clock.now += 100.0  # a long idle would refill far past capacity if uncapped

    bucket.acquire()
    bucket.acquire()  # only `burst` tokens accumulated, both immediate
    assert clock.now == 100.0

    bucket.acquire()  # the third is throttled — capacity was capped at 2
    assert abs(clock.now - 100.2) < 1e-9

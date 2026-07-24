"""Retries, backoff, and rate limiting (ARCHITECTURE.md §13).

External calls — GitHub and the model — fail transiently, so they are wrapped in
exponential backoff with a bounded retry count and typed give-up behavior (a
retry that never gives up is just a slower infinite loop). A token-bucket limiter
caps the rate of external calls so limpiador is not throttled or banned during a
busy run. Retry counts, backoff base, and bucket size are named configuration
(CLEAN_CODE.md §7); give-up raises an exhausted ``TransientError`` (errors.py).

These utilities are deliberately provider-agnostic. The retry triggers on the
typed :class:`~limpiador.observability.errors.TransientError`, so each boundary
(the model adapter, the GitHub tools) translates its own transient provider
failures into that one signal and wraps the call here — the policy lives in one
place instead of being scattered across call sites.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from limpiador.observability.errors import TransientError

T = TypeVar("T")

# Injection seams: the wall clock and the wait. Tests pass a fake clock whose
# ``sleep`` only advances time, so backoff and throttling are deterministic.
Clock = Callable[[], float]
Sleep = Callable[[float], None]


# ---- bounded retry with exponential backoff ---------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    """How hard to retry a transient failure before giving up — named config.

    ``max_attempts`` counts the first try plus retries. Backoff is exponential:
    the wait before retry *n* is ``base_delay_s * 2**(n-1)``, capped at
    ``max_delay_s`` so it cannot grow without bound.
    """

    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def delay_for(self, failed_attempt: int) -> float:
        """The wait after ``failed_attempt`` (1-based) fails, before the next try."""
        return min(self.max_delay_s, self.base_delay_s * (2 ** (failed_attempt - 1)))


DEFAULT_RETRY = RetryPolicy()


def retrying(fn: Callable[[], T], *, policy: RetryPolicy = DEFAULT_RETRY, sleep: Sleep = time.sleep) -> T:
    """Call ``fn``, retrying a :class:`TransientError` per ``policy``.

    A transient failure backs off and retries up to ``max_attempts``; on the last
    one it raises an *exhausted* ``TransientError`` chaining the final cause — a
    typed give-up, never an unbounded loop. Any non-transient exception propagates
    immediately, unretried: only the transient signal is worth retrying.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except TransientError as error:
            if attempt >= policy.max_attempts:
                raise TransientError(
                    f"gave up after {attempt} attempt(s); last transient failure: {error}"
                ) from error
            sleep(policy.delay_for(attempt))
    raise AssertionError("unreachable: the loop returns or raises on every path")


# ---- token-bucket rate limiter ----------------------------------------------
@dataclass(frozen=True)
class RateLimit:
    """The cap on external-call rate — named config (CLEAN_CODE.md §7).

    ``burst`` tokens may be spent immediately; the bucket then refills at
    ``rate_per_second`` tokens a second, so sustained throughput is the rate while
    a short spike up to the burst is still allowed.
    """

    rate_per_second: float
    burst: int


DEFAULT_RATE_LIMIT = RateLimit(rate_per_second=5.0, burst=10)


class TokenBucket:
    """A token-bucket limiter: allow a burst, then meter to the configured rate.

    The bucket starts full. :meth:`acquire` refills by the elapsed time, and if a
    token is not yet available it sleeps exactly long enough for one to accrue.
    Clock and sleep are injected, so a fake clock makes throttling deterministic.
    """

    def __init__(self, limit: RateLimit, *, clock: Clock = time.monotonic, sleep: Sleep = time.sleep) -> None:
        self._rate = limit.rate_per_second
        self._capacity = float(limit.burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(limit.burst)  # start full: the first burst is free
        self._last = clock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Spend ``tokens``, sleeping until enough have accrued at the configured rate."""
        self._refill()
        if self._tokens < tokens:
            wait = (tokens - self._tokens) / self._rate
            self._sleep(wait)
            self._refill()
        self._tokens -= tokens

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)


# ---- combined boundary helper -----------------------------------------------
@dataclass(frozen=True)
class Resilience:
    """The resilience configuration applied at an external-call boundary.

    Bundles the retry policy, the rate limit, and the clock/sleep seams so a
    boundary (the model adapter, the GitHub tools) takes one value and a test can
    inject a fake clock to drive both backoff and throttling deterministically.
    """

    retry: RetryPolicy = DEFAULT_RETRY
    rate_limit: RateLimit = DEFAULT_RATE_LIMIT
    sleep: Sleep = time.sleep
    clock: Clock = time.monotonic

    def bucket(self) -> TokenBucket:
        """A fresh token bucket for this configuration."""
        return TokenBucket(self.rate_limit, clock=self.clock, sleep=self.sleep)


def resilient_call(
    fn: Callable[[], T],
    *,
    limiter: TokenBucket,
    policy: RetryPolicy = DEFAULT_RETRY,
    sleep: Sleep = time.sleep,
) -> T:
    """Run ``fn`` through the rate limiter and the retry policy.

    A token is acquired before *each* attempt — including retries — so a retried
    call is throttled like any other, and the whole boundary's resilience is this
    one wrapper rather than logic scattered across call sites.
    """

    def attempt() -> T:
        limiter.acquire()
        return fn()

    return retrying(attempt, policy=policy, sleep=sleep)

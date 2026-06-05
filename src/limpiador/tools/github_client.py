"""The GitHub external-call boundary (ARCHITECTURE.md §5.3, §13).

Every ``github.*`` tool's call to the GitHub API goes through this one boundary,
so the retry/backoff and rate limiting live in a single place — applied here, not
scattered across the fourteen tools. Transient GitHub failures (a rate-limit
response, a 5xx) and dropped connections are translated into the typed
``TransientError`` the shared retry backs off and retries; a non-transient failure
(a 404, a 422 validation error, a bad credential) propagates unretried, for the
agent to read and adapt to.

This mirrors the model adapter's boundary (agent/llm.py): the resilience policy
is the shared one from retry.py; only the provider-specific translation of
"which failures are transient" lives here, next to the GitHub SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import requests
from github import GithubException, RateLimitExceededException

from limpiador.observability.errors import TransientError
from limpiador.observability.retry import Resilience, resilient_call

T = TypeVar("T")

# GitHub's transient HTTP statuses: a secondary-rate-limit 429 and 5xx server
# errors. A 404 / 422 / 401 is the caller's mistake to read, not a retry's to fix.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Connection-level failures from the underlying HTTP client are always transient.
_TRANSIENT_CONNECTION_ERRORS = (requests.ConnectionError, requests.Timeout)


def is_transient_status(status: int) -> bool:
    """Whether a GitHub HTTP status is worth backing off and retrying."""
    return status in _TRANSIENT_STATUSES


class GitHubBoundary:
    """The single chokepoint ``github.*`` tools route their API calls through.

    Holds its own token bucket and retry policy — GitHub's rate limits are
    independent of the model's — and translates transient provider failures into
    the typed ``TransientError`` the shared retry understands.
    """

    def __init__(self, *, resilience: Resilience | None = None) -> None:
        self._resilience = resilience or Resilience()
        self._retry = self._resilience.retry
        self._sleep = self._resilience.sleep
        self._limiter = self._resilience.bucket()

    def call(self, operation: Callable[[], T]) -> T:
        """Run one GitHub API ``operation`` rate-limited and retried.

        A rate-limit response is always transient; a generic ``GithubException``
        is transient only on a 429/5xx status; a dropped connection is transient.
        Anything else propagates unretried.
        """

        def attempt() -> T:
            try:
                return operation()
            except RateLimitExceededException as error:
                raise TransientError(f"github rate limit: {error}") from error
            except GithubException as error:
                if is_transient_status(error.status):
                    raise TransientError(f"github transient failure ({error.status}): {error}") from error
                raise
            except _TRANSIENT_CONNECTION_ERRORS as error:
                raise TransientError(f"github connection failure: {error}") from error

        return resilient_call(attempt, limiter=self._limiter, policy=self._retry, sleep=self._sleep)

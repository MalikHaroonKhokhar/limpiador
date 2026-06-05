"""Shared test fixtures and collection policy (ARCHITECTURE.md §11).

This is where the temp-git-repo fixtures, the deterministic mock LLM, and the
loaded-registry fixture will live as the suite fills in over later tickets. For
the bootstrap skeleton it carries one piece of collection policy.
"""

from __future__ import annotations

import pytest

# Pytest's "no tests collected" exit code. The layered suites (tests/unit/,
# tests/integration/) start empty in the skeleton and fill in later; an empty
# collection there must not turn `make test` red on an otherwise-green run.
_NO_TESTS_COLLECTED = 5


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Treat an empty collection as success, not failure.

    Bootstrap acceptance requires `make test` to run green on an empty suite.
    Without this, running an empty layer directory would exit 5 and fail the
    target even though nothing is actually broken.
    """
    if exitstatus == _NO_TESTS_COLLECTED:
        session.exitstatus = 0

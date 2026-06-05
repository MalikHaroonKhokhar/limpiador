"""Shared test fixtures and collection policy (ARCHITECTURE.md §11).

This is where the temp-git-repo fixtures, the deterministic mock LLM, and the
loaded-registry fixture will live as the suite fills in over later tickets. For
the bootstrap skeleton it carries one piece of collection policy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Put tests/ on the import path so the test-support layer (tests/support/) is
# importable as `support`. The mock LLM and scenario helpers live there — never
# under src/limpiador/ — and are injected through the LLMAdapter interface
# (ARCHITECTURE.md §10, .clauderules §5).
_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# Importing the test-support layer registers the mock adapter, so
# LIMPIADOR_LLM=mock resolves to it across the whole session (the run mode the
# Makefile sets for `make test`). Production never imports this.
import support  # noqa: E402,F401 — registration side effect, after sys.path setup

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

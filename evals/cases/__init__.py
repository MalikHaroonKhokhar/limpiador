"""The eval cases — one module per reasoning behaviour (ARCHITECTURE.md §11.3).

Each case binds a committed fixture with a *known* seeded defect to a task and to
two binary assertions: ``check_outcome`` (did the goal get achieved) and
``check_trace`` (did the agent reason well, in the right order, under the
ceiling). Most cases drive the main agent loop; ``catch_regression`` drives the
reviewer subagent instead (``kind = REVIEWER``). The harness reads each case's
``kind`` to choose the runner.

The headline is ``red_herring``: an innocent recently-refactored file sits beside
the truly-broken one, so a naive recency heuristic genuinely fails the case.
"""

from __future__ import annotations

from evals.cases import catch_regression, fix_failing_test, red_herring, safe_rename
from evals.cases._base import AGENT, REVIEWER, EvalCase

# Ordered cheapest-to-deepest signal: fix, rename, review, then the red herring.
CASES: tuple[EvalCase, ...] = (
    fix_failing_test.CASE,
    safe_rename.CASE,
    catch_regression.CASE,
    red_herring.CASE,
)

__all__ = ["AGENT", "CASES", "REVIEWER", "EvalCase"]

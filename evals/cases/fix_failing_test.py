"""Case: fix a failing test (fixture ``failing_test``).

The repo has one planted bug — ``calc.add`` subtracts instead of adding — and one
failing test. A well-reasoning agent reproduces the failure, locates the cause,
fixes it, and re-runs the suite to confirm.

* **outcome** — the suite is green.
* **trace** — the agent ran the tests (it verified rather than fixing blind); the
  order is left unconstrained so a fix-then-verify sequence is not punished.
"""

from __future__ import annotations

from pathlib import Path

from limpiador.observability.tracing import Tracer

from evals.cases._base import EvalCase, run_tests

_TASK = (
    "A test in this repository is failing. Find the cause, fix it so all tests "
    "pass, and do not change any file that is not the cause of the bug."
)


def _outcome(checkout: Path) -> list[str]:
    return run_tests(checkout)


def _trace(tracer: Tracer) -> list[str]:
    if not tracer.called("test_run_tests"):
        return ["the agent never ran the tests to locate the cause or verify the fix"]
    return []


CASE = EvalCase(
    name="fix_failing_test",
    fixture="failing_test",
    task=_TASK,
    check_outcome=_outcome,
    check_trace=_trace,
    max_calls=30,
)

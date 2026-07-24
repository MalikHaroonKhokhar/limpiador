"""Case: resist the red herring (fixture ``red_herring``).

Two files look suspicious. ``settings.py`` is the innocent one — recently
refactored, the obvious suspect a recency- or blame-driven agent reaches for
first — but it is correct. The real defect is in ``pipeline.normalize``
(``rows[1:]`` silently drops the first row). A well-reasoning agent gathers
evidence, fixes the true cause, and leaves the herring byte-for-byte unchanged.

This is the strongest depth signal in the suite: a naive recency heuristic edits
``settings.py`` and *fails* the case (see the deliberately-wrong variant in
``tests/unit/test_eval_cases.py``).

* **outcome** — ``pipeline.py`` (the real cause) was changed, ``settings.py`` (the
  herring) was left untouched, and the suite is green.
* **trace** — the agent reproduced the failure with the tests before editing; it
  reasoned from evidence rather than pattern-matching recency.
"""

from __future__ import annotations

from pathlib import Path

from evals.cases._base import EvalCase, file_changed, file_unchanged, run_tests
from limpiador.observability.tracing import Tracer

_FIXTURE = "red_herring"
_TASK = (
    "A test in this repository is failing. Find the file that is actually the "
    "cause, fix it so all tests pass, and do not change any file that is not the "
    "cause of the bug."
)


def _outcome(checkout: Path) -> list[str]:
    return (
        run_tests(checkout)
        + file_changed(checkout, _FIXTURE, "pipeline.py")
        + file_unchanged(checkout, _FIXTURE, "settings.py")
    )


def _trace(tracer: Tracer) -> list[str]:
    if not tracer.called("test_run_tests"):
        return ["the agent fixed blind — it never ran the tests to gather evidence"]
    return []


CASE = EvalCase(
    name="red_herring",
    fixture=_FIXTURE,
    task=_TASK,
    check_outcome=_outcome,
    check_trace=_trace,
    max_calls=30,
)

"""Eval cases — one per reasoning behaviour under test (ARCHITECTURE.md §11.3).

Each case runs the real agent against a committed fixture with a *known* seeded
defect, so ground truth is binary, and asserts on **both** layers:

* **outcome** — did the goal actually get achieved (the tests pass, the symbol is
  renamed everywhere, the red herring was left alone);
* **trace** — did the agent reason well to get there (it verified with the tests,
  it resolved references before renaming) — read from the run's :class:`Tracer`
  via its ``called`` / ``order`` helpers (HAR-14).

Tool names in the trace are the OpenAI-safe form (``ast_find_references``), which
is what the loop records, so the trace checks use that form.

``fix_failing_test`` carries the mandated planted **red herring**: an innocent
``formatter.py`` beside the truly-broken ``calc.py``; a well-reasoning agent
fixes the cause and leaves the herring byte-for-byte unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from limpiador.observability.tracing import Tracer

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class EvalCase:
    """One reasoning behaviour: a fixture, a task, and the two-layer assertions."""

    name: str
    fixture: str
    task: str
    check_outcome: Callable[[Path], list[str]]
    check_trace: Callable[[Tracer], list[str]]
    max_calls: int = 30


# ---- outcome checks (on the mutated checkout) -------------------------------
def _tests_pass(checkout: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(checkout)],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return []
    return [f"the test suite still fails after the fix:\n{proc.stdout[-400:]}"]


def _red_herring_untouched(checkout: Path) -> list[str]:
    pristine = (_FIXTURES / "failing_test" / "formatter.py").read_text()
    if (checkout / "formatter.py").read_text() == pristine:
        return []
    return ["the red-herring file formatter.py was modified — it is not the cause"]


def _failing_test_outcome(checkout: Path) -> list[str]:
    return _tests_pass(checkout) + _red_herring_untouched(checkout)


def _rename_outcome(checkout: Path) -> list[str]:
    core = (checkout / "pkg" / "core.py").read_text()
    failures: list[str] = []
    if "def compute(" in core:
        failures.append("compute is still defined under its old name in core.py")
    if "calculate" not in core:
        failures.append("the new name 'calculate' is absent from core.py")
    # consumer.py and report.py are the two call-site files; both must be reached.
    for site in ("consumer.py", "report.py"):
        source = (checkout / "pkg" / site).read_text()
        if "compute(" in source:
            failures.append(f"the call site in {site} was not renamed")
        if "calculate(" not in source:
            failures.append(f"{site} does not call the renamed function")
    return failures


# ---- trace checks (on the run's Tracer) -------------------------------------
def _failing_test_trace(tracer: Tracer) -> list[str]:
    if not tracer.called("test_run_tests"):
        return ["the agent never ran the tests to verify its fix"]
    return []


def _rename_trace(tracer: Tracer) -> list[str]:
    if not tracer.called("ast_find_references"):
        return ["the agent renamed without resolving references first"]
    if tracer.called("ast_rename_symbol") and not tracer.order(
        "ast_find_references", "ast_rename_symbol"
    ):
        return ["references were resolved AFTER the rename, not before it"]
    return []


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="fix_failing_test",
        fixture="failing_test",
        task=(
            "A test in this repository is failing. Find the cause, fix it so all "
            "tests pass, and do not change any file that is not the cause of the bug."
        ),
        check_outcome=_failing_test_outcome,
        check_trace=_failing_test_trace,
        max_calls=30,
    ),
    EvalCase(
        name="rename_symbol_across_files",
        fixture="rename_symbol",
        task=(
            "Rename the function `compute` to `calculate` everywhere it is defined "
            "or used across this package, without breaking any call site."
        ),
        check_outcome=_rename_outcome,
        check_trace=_rename_trace,
        max_calls=28,
    ),
)

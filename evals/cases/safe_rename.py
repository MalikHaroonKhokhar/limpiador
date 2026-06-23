"""Case: rename a symbol safely across files (fixture ``rename_symbol``).

``compute`` is defined in ``pkg/core.py`` and called from ``pkg/consumer.py`` and
``pkg/report.py`` — three known sites. A safe rename reaches every site and leaves
the package importable.

* **outcome** — the old name is gone and the new name present at all three sites,
  *and* the suite is green (a missed call site breaks the public-API smoke test).
* **trace** — the agent resolved references with ``ast.find_references`` *before*
  it renamed, not after — it mapped the blast radius first.
"""

from __future__ import annotations

from pathlib import Path

from limpiador.observability.tracing import Tracer

from evals.cases._base import EvalCase, run_tests

_TASK = (
    "Rename the function `compute` to `calculate` everywhere it is defined or used "
    "across this package, without breaking any call site."
)


def _rename_complete(checkout: Path) -> list[str]:
    failures: list[str] = []
    core = (checkout / "pkg" / "core.py").read_text()
    if "def compute(" in core:
        failures.append("compute is still defined under its old name in core.py")
    if "calculate" not in core:
        failures.append("the new name 'calculate' is absent from core.py")
    for site in ("consumer.py", "report.py"):
        source = (checkout / "pkg" / site).read_text()
        if "compute(" in source:
            failures.append(f"the call site in {site} was not renamed")
        if "calculate(" not in source:
            failures.append(f"{site} does not call the renamed function")
    return failures


def _outcome(checkout: Path) -> list[str]:
    return _rename_complete(checkout) + run_tests(checkout)


def _trace(tracer: Tracer) -> list[str]:
    if not tracer.called("ast_find_references"):
        return ["the agent renamed without resolving references first"]
    if tracer.called("ast_rename_symbol") and not tracer.order(
        "ast_find_references", "ast_rename_symbol"
    ):
        return ["references were resolved AFTER the rename, not before it"]
    return []


CASE = EvalCase(
    name="safe_rename",
    fixture="rename_symbol",
    task=_TASK,
    check_outcome=_outcome,
    check_trace=_trace,
    max_calls=28,
)

"""Eval harness entrypoint (ARCHITECTURE.md §11.3).

Runs each eval case against a *fresh checkout* of its committed fixture (fixture
isolation is mandatory — one run's edits must not poison the next, and the
committed original is never touched), drives the real agent against the seeded
defect, and asserts on both layers: outcome (goal achieved) and trace (reasoned
in the right order, under the call ceiling). Invoked by ``make eval`` as
``python -m evals.harness`` in REAL mode; it skips cleanly when ``OPENAI_API_KEY``
is unset, so it is safe to invoke unconfigured.

This is the distinct concern from ``tests/``: the test suite asks whether the
*code* works; the eval harness asks whether the *agent reasons well*.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from git import Repo

from limpiador.agent.guard import CallGuard
from limpiador.agent.llm import build_adapter
from limpiador.agent.loop import run
from limpiador.cli import _system_prompt
from limpiador.observability.tracing import Tracer
from limpiador.tools.registry import build_registry

from evals.cases import CASES, EvalCase
from evals.report import render

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_HARNESS_IDENTITY = ("Eval Harness", "eval@example.com")


@dataclass(frozen=True)
class EvalResult:
    """One case's two-layer verdict and the tool-call trace it produced."""

    name: str
    passed: bool
    outcome_failures: list[str]
    trace_failures: list[str]
    tool_calls: tuple[str, ...]


def checkout_fixture(fixture: str) -> Path:
    """A fresh, isolated git checkout of a committed fixture — never the original.

    The fixture's files are copied into a throwaway temp directory and turned into
    a real git repo (so the agent's git tools work), leaving the committed
    ``evals/fixtures/<fixture>`` pristine. The caller is responsible for removing
    the returned directory.
    """
    destination = Path(tempfile.mkdtemp(prefix=f"eval-{fixture}-"))
    shutil.copytree(_FIXTURES / fixture, destination, dirs_exist_ok=True)
    repo = Repo.init(destination)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", _HARNESS_IDENTITY[0])
        writer.set_value("user", "email", _HARNESS_IDENTITY[1])
    repo.git.add(A=True)
    repo.index.commit("eval fixture seed")
    return destination


@contextmanager
def _in_dir(path: Path) -> Iterator[None]:
    """Run a block with the cwd moved into ``path`` (the tools resolve cwd)."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def evaluate_case(case: EvalCase) -> EvalResult:
    """Run one case against a fresh checkout and score it on outcome and trace."""
    checkout = checkout_fixture(case.fixture)
    try:
        tracer = Tracer()
        with _in_dir(checkout):
            outcome = run(
                case.task,
                registry=build_registry(),
                adapter=build_adapter(),
                guard=CallGuard(ceiling=case.max_calls),
                system_prompt=_system_prompt(checkout),
                tracer=tracer,
            )
        if outcome.aborted:
            outcome_failures = [
                f"run aborted: hit the {case.max_calls}-call ceiling without finishing"
            ]
        else:
            outcome_failures = case.check_outcome(checkout)
        trace_failures = case.check_trace(tracer)
        return EvalResult(
            name=case.name,
            passed=not (outcome_failures or trace_failures),
            outcome_failures=outcome_failures,
            trace_failures=trace_failures,
            tool_calls=outcome.tool_calls,
        )
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def run_all(cases: Sequence[EvalCase] = CASES) -> list[EvalResult]:
    """Evaluate every case in order, each on its own isolated checkout."""
    return [evaluate_case(case) for case in cases]


def main(argv: list[str] | None = None) -> int:
    """Run the eval suite and return a process exit code (0 = all passed)."""
    if not os.environ.get("OPENAI_API_KEY"):
        print("⏭️  Evals skipped: OPENAI_API_KEY not set (the eval harness runs the real model).")
        return 0
    results = run_all()
    print(render(results))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())

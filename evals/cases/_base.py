"""Shared scaffolding for the eval cases (ARCHITECTURE.md §11.3).

An :class:`EvalCase` binds a committed fixture to a task and to two binary
assertions: ``check_outcome`` (did the goal get achieved) and ``check_trace`` (did
the agent reason well, in the right order, under the ceiling). Most cases drive
the main agent loop (``kind = AGENT``); the reviewer case drives the reviewer
subagent instead (``kind = REVIEWER``) and so carries the PR ``diff_file`` and the
``changed_files`` it touches — the harness reads ``kind`` to pick the runner.

The helpers here are the assertion primitives the case modules compose:
``run_tests`` (the suite is green), ``file_unchanged`` (an innocent file was left
alone), and ``reviewer_read_only_names`` (the authoritative read-only allow-list a
trace is checked against).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from limpiador.observability.tracing import Tracer

# Case kinds: which engine the harness drives the case against.
AGENT = "agent"
REVIEWER = "reviewer"

# The committed fixtures live one level up, in evals/fixtures/.
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@dataclass(frozen=True)
class EvalCase:
    """One reasoning behaviour: a fixture, a task, and the two-layer assertions.

    ``check_outcome`` receives the *subject* the case produces — the mutated
    checkout :class:`~pathlib.Path` for an agent case, or the
    :class:`~limpiador.schemas.ReviewResult` for the reviewer case.
    """

    name: str
    fixture: str
    task: str
    check_outcome: Callable[[Any], list[str]]
    check_trace: Callable[[Tracer], list[str]]
    max_calls: int = 30
    kind: str = AGENT
    # Reviewer cases only: the fixture-relative PR diff and the files it changes.
    diff_file: str = ""
    changed_files: tuple[str, ...] = field(default_factory=tuple)


# ---- outcome primitives -----------------------------------------------------
def run_tests(checkout: Path) -> list[str]:
    """No failures iff the checkout's pytest suite is green."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(checkout)],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return []
    return [f"the test suite still fails:\n{proc.stdout[-400:]}"]


def file_unchanged(checkout: Path, fixture: str, relative: str) -> list[str]:
    """No failures iff ``relative`` is byte-for-byte identical to the committed
    fixture — i.e. an innocent file was left alone."""
    pristine = (FIXTURES / fixture / relative).read_text()
    if (checkout / relative).read_text() == pristine:
        return []
    return [f"the file {relative} was modified — it is not the cause"]


def file_changed(checkout: Path, fixture: str, relative: str) -> list[str]:
    """No failures iff ``relative`` differs from the committed fixture — i.e. the
    file that *should* have been fixed actually was."""
    pristine = (FIXTURES / fixture / relative).read_text()
    if (checkout / relative).read_text() != pristine:
        return []
    return [f"the file {relative} (the real cause) was never changed"]


# ---- trace primitives -------------------------------------------------------
def reviewer_read_only_names() -> frozenset[str]:
    """The OpenAI-safe names a reviewer is *allowed* to call — its read-only
    registry plus the three core meta-tools. A trace check asserts the reviewer
    stayed within this set."""
    from limpiador.subagents.reviewer import build_reviewer_registry
    from limpiador.tools.registry import CORE_TOOL_NAMES

    registry = build_reviewer_registry()
    names = {name.replace(".", "_") for name in registry.tool_names()}
    names |= {name.replace(".", "_") for name in CORE_TOOL_NAMES}
    return frozenset(names)

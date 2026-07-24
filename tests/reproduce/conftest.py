"""Shared rig for the reproduction tier (real model).

Every reproduction test drives the *production* agent — the full 56-tool
registry, the real OpenAI adapter, and the same system prompt the CLI uses — on
a tiny seeded repo, then asserts on the behaviour the run exhibited. The whole
tier skips cleanly when ``OPENAI_API_KEY`` is unset, so CI stays green and no
credit is spent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from limpiador.agent.guard import CallGuard
from limpiador.agent.llm import build_adapter
from limpiador.agent.loop import RunResult, run
from limpiador.cli import _system_prompt
from limpiador.tools.registry import build_registry


@pytest.fixture(autouse=True)
def _require_openai_key() -> None:
    """Skip the whole reproduction tier unless a real key is present."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("reproduction tier is real-model; set OPENAI_API_KEY to run it")


@pytest.fixture(autouse=True)
def _anchor_github_to_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``github.*`` tools at the configured throwaway repo.

    The tier drives the *whole* 57-tool registry, so the agent may reach for a
    ``github.*`` tool at any point. A seeded temp repo has no origin, and without
    a slug those tools raise a **fatal** ConfigError that kills the run outright —
    losing the behaviour under test to an unrelated crash. Anchoring
    ``GITHUB_REPOSITORY`` to ``LIMPIADOR_SANDBOX_REPO`` (the throwaway repo this
    project already designates for real-mode work) makes such a call resolve and
    fail recoverably instead. Reproduction tasks are local, so it is not called in
    practice; when no sandbox is configured, nothing is set.
    """
    sandbox = os.environ.get("LIMPIADOR_SANDBOX_REPO")
    if sandbox and not os.environ.get("GITHUB_REPOSITORY"):
        monkeypatch.setenv("GITHUB_REPOSITORY", sandbox)


@pytest.fixture
def run_agent():
    """Drive the production agent on the current repo and return its RunResult.

    Uses an *isolated* full registry (not the shared singleton, so one run's
    loaded-tool state never bleeds into another) and the CLI's own system prompt
    anchored to the seeded repo, so the run reproduces what production does. A low
    call ceiling bounds the credits a real run can spend.
    """

    def _drive(task: str, *, ceiling: int = 16) -> RunResult:
        return run(
            task,
            registry=build_registry(),
            adapter=build_adapter(),
            guard=CallGuard(ceiling=ceiling),
            system_prompt=_system_prompt(Path.cwd()),
        )

    return _drive

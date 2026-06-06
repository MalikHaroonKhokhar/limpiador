"""Shared test harness — the fixtures every feature ticket's tests lean on
(ARCHITECTURE.md §11).

This is pure infrastructure, not a feature: it *is* the harness. It centralises
the three things the layered suites reach for over and over, so a feature test
asks for a fixture instead of re-deriving it:

* **Temp git repos** — :func:`make_git_repo` (a factory) and :func:`git_repo` (a
  ready, seeded, committed repo with the cwd inside it). The git/fs/ast tools
  resolve the repository ambiently from the working directory, so a fixture that
  ``chdir``-s into a fresh temp repo is what anchors them — exactly how the CLI
  anchors the agent to ``--repo``.
* **The deterministic mock LLM** — :func:`mock_adapter` bundles the ``MockLLM``
  builder with the scenario-authoring helpers, so a test scripts a run without
  importing the support layer by hand.
* **A pre-loaded registry** — :func:`make_loaded_registry` registers *and* loads
  a set of tools, so a loop test can dispatch immediately (discovery is
  unit-tested elsewhere).

It also provides :func:`checkout_fresh_fixture`: a pristine copy of a golden
tree per call, auto-cleaned with ``tmp_path``, so a test that mutates a tree is
structurally isolated from the next.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from git import Repo

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
from support.mock_llm import (  # noqa: E402
    MockLLM,
    final_turn,
    scenario,
    tool_call,
    tool_turn,
)

from limpiador.tools.base import Tool  # noqa: E402
from limpiador.tools.registry import ToolRegistry  # noqa: E402

# A fixed identity for every harness repo, so commits are deterministic and a
# test never depends on the developer's global git config.
_HARNESS_NAME = "Test Harness"
_HARNESS_EMAIL = "harness@example.com"

# The golden fixture tree checkout_fresh_fixture copies from.
_FIXTURES_DIR = _TESTS_DIR / "fixtures"


# ---- temp git repos ---------------------------------------------------------
def _configure_identity(repo: Repo) -> None:
    """Give a repo a deterministic commit identity (not the developer's global)."""
    with repo.config_writer() as writer:
        writer.set_value("user", "name", _HARNESS_NAME)
        writer.set_value("user", "email", _HARNESS_EMAIL)


@pytest.fixture
def make_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Repo]:
    """Factory: a configured temp git repo, optionally seeded and committed.

    ``files`` maps repo-relative paths to contents; when given and ``commit`` is
    set, they are staged and committed. ``chdir`` (default True) moves the cwd
    inside the repo so the ambient-resolution tools act on it. Each call gets its
    own directory under ``tmp_path``, so two repos in one test never collide.
    """
    made: list[Repo] = []

    def factory(
        files: dict[str, str] | None = None,
        *,
        commit: str | None = "initial commit",
        chdir: bool = True,
    ) -> Repo:
        root = tmp_path / f"repo{len(made)}"
        root.mkdir()
        repo = Repo.init(root)
        _configure_identity(repo)
        for relative, content in (files or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if files and commit:
            repo.index.add(list(files))
            repo.index.commit(commit)
        if chdir:
            monkeypatch.chdir(root)
        made.append(repo)
        return repo

    return factory


@pytest.fixture
def git_repo(make_git_repo: Callable[..., Repo]) -> Repo:
    """A ready, seeded, committed temp git repo with the cwd inside it."""
    return make_git_repo({"README.md": "# sandbox\n", "src/app.py": "VALUE = 1\n"})


# ---- the deterministic mock LLM ---------------------------------------------
@pytest.fixture
def mock_adapter() -> SimpleNamespace:
    """The mock LLM builder plus the scenario-authoring helpers, in one namespace.

    ``build(*turns)`` returns a :class:`MockLLM`; ``tool_call`` / ``tool_turn`` /
    ``final_turn`` author the turns. So a test scripts a run as
    ``mock_adapter.build(mock_adapter.tool_turn(mock_adapter.tool_call(...)),
    mock_adapter.final_turn("done"))`` without importing the support layer.
    """
    return SimpleNamespace(
        build=lambda *turns: MockLLM(scenario(*turns)),
        tool_call=tool_call,
        tool_turn=tool_turn,
        final_turn=final_turn,
    )


# ---- a pre-loaded registry --------------------------------------------------
@pytest.fixture
def make_loaded_registry() -> Callable[..., ToolRegistry]:
    """Factory: a registry with the given tools registered *and loaded*.

    Discovery (search → load) is unit-tested in its own suite; a loop or
    composition test usually just wants the tools dispatchable, which is what
    pre-loading gives it.
    """

    def factory(*tools: Tool) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
            registry.load({"name": tool.name})
        return registry

    return factory


# ---- a pristine fixture copy per run ----------------------------------------
@pytest.fixture
def pristine_sample() -> Path:
    """The path to the read-only golden fixture tree (copy it, never mutate it)."""
    return _FIXTURES_DIR / "sample_project"


@pytest.fixture
def checkout_fresh_fixture(tmp_path: Path) -> Callable[[Path], Path]:
    """Factory: copy a pristine fixture tree into a fresh temp dir per call.

    Each checkout is an independent copy under ``tmp_path`` (auto-removed at
    teardown), so a test that mutates its checkout cannot affect any other — the
    isolation guarantee the suite relies on.
    """
    taken: list[Path] = []

    def checkout(source: Path) -> Path:
        destination = tmp_path / f"fixture{len(taken)}"
        shutil.copytree(source, destination)
        taken.append(destination)
        return destination

    return checkout


@pytest.fixture
def fresh_sample(checkout_fresh_fixture: Callable[[Path], Path], pristine_sample: Path) -> Path:
    """A fresh, mutable copy of the golden sample tree, isolated per test."""
    return checkout_fresh_fixture(pristine_sample)


# ---- collection policy ------------------------------------------------------
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

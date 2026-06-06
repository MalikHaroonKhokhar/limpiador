"""Self-tests for the shared harness (HAR-24).

The harness is pure infrastructure — it has no feature behaviour to fold tests
around, so these *are* its tests: each shared fixture is exercised once to prove
it does what every feature ticket relies on, and a pair of ordered tests proves
the isolation guarantee — a test that mutates its tree does not affect the next.
"""

from __future__ import annotations

from pathlib import Path

from git import Repo

from limpiador.tools import fs_tools
from limpiador.tools.registry import CORE_TOOL_NAMES


def _fs_tool(name: str):
    return next(tool for tool in fs_tools.TOOLS if tool.name == name)


# ---- temp git repo fixtures -------------------------------------------------
def test_git_repo_is_seeded_committed_and_current(git_repo) -> None:
    assert isinstance(git_repo, Repo)
    assert len(list(git_repo.iter_commits())) == 1  # the seeded commit
    # The cwd is inside the repo — what anchors the ambient-resolution tools.
    assert Path(git_repo.working_tree_dir).resolve() == Path.cwd().resolve()
    assert (Path.cwd() / "src" / "app.py").read_text() == "VALUE = 1\n"


def test_make_git_repo_seeds_files_and_commits_them(make_git_repo) -> None:
    repo = make_git_repo({"a.txt": "alpha\n", "pkg/b.py": "B = 2\n"}, commit="seed two")

    assert repo.head.commit.message == "seed two"
    assert not repo.is_dirty(untracked_files=True)  # everything was committed
    assert (Path(repo.working_tree_dir) / "pkg" / "b.py").read_text() == "B = 2\n"


def test_make_git_repo_can_build_an_uncommitted_tree(make_git_repo) -> None:
    repo = make_git_repo({"draft.txt": "wip\n"}, commit=None)
    assert "draft.txt" in repo.untracked_files


# ---- the deterministic mock LLM ---------------------------------------------
def test_mock_adapter_builds_and_replays_a_scenario(mock_adapter) -> None:
    mock = mock_adapter.build(
        mock_adapter.tool_turn(mock_adapter.tool_call("finish", {"result": "done"})),
        mock_adapter.final_turn("all done"),
    )

    first = mock.complete([], tools=[])
    assert first.tool_calls[0].name == "finish"
    second = mock.complete([], tools=[])
    assert second.text == "all done"


# ---- a pre-loaded registry --------------------------------------------------
def test_make_loaded_registry_registers_and_loads(make_loaded_registry) -> None:
    registry = make_loaded_registry(_fs_tool("fs.read_file"), _fs_tool("fs.grep"))

    assert set(registry.loaded_names()) == {"fs.read_file", "fs.grep"}
    # Loaded tools are immediately dispatchable: they appear in the active schemas
    # alongside the fixed core.
    assert len(registry.active_schemas()) == len(CORE_TOOL_NAMES) + 2


# ---- a pristine fixture copy per run ----------------------------------------
def test_checkout_fresh_fixture_is_an_independent_copy(
    checkout_fresh_fixture, pristine_sample
) -> None:
    copy = checkout_fresh_fixture(pristine_sample)

    assert copy != pristine_sample
    assert (copy / "src" / "app.py").read_text() == (pristine_sample / "src" / "app.py").read_text()

    # Two checkouts of the same source are independent directories.
    other = checkout_fresh_fixture(pristine_sample)
    assert other != copy


# ---- isolation: an ordered pair proves a mutation does not bleed ------------
_MUTATION_MARKER = "MUTATED_BY_STEP_1.txt"


def test_isolation_step_1_mutates_its_own_copy(fresh_sample) -> None:
    (fresh_sample / _MUTATION_MARKER).write_text("step 1 was here\n")
    (fresh_sample / "src" / "app.py").write_text("CORRUPTED = True\n")

    assert (fresh_sample / _MUTATION_MARKER).exists()


def test_isolation_step_2_gets_a_pristine_copy(fresh_sample, pristine_sample) -> None:
    # Step 2's copy is untouched by step 1 — the whole point of the harness.
    assert not (fresh_sample / _MUTATION_MARKER).exists()
    app = (fresh_sample / "src" / "app.py").read_text()
    assert "def greet(name):" in app
    assert "CORRUPTED" not in app
    # And the golden source itself was never mutated by step 1.
    assert not (pristine_sample / _MUTATION_MARKER).exists()
    assert "CORRUPTED" not in (pristine_sample / "src" / "app.py").read_text()

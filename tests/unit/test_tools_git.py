"""Unit tests for the ``git.*`` namespace — local repository state (HAR-17).

This is the first real tool namespace, so it is where the registry pattern is
proven *at scale* against a live external surface (gitpython over a real repo).
Each of the twelve tools is held to the same contract the abstract base
promises: given a seeded temp repo it returns the right *typed* object on the
happy path, and on failure it raises the *correct typed* ``ToolError`` — never a
raw gitpython exception, never free text, never a sentinel.

The tests also pin the namespace into the registry the same way every other
tool is reached — all twelve are searchable and loadable, none leak into the
fixed core — and assert the CLEAN_CODE.md §2 size budget on every function in
the module so the executors stay single-purpose as they land.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from git import Repo

from limpiador.observability.errors import (
    MalformedInputError,
    NotFoundError,
    ToolError,
)
from limpiador.schemas import (
    GitBlameResult,
    GitBranchCreateResult,
    GitBranchListResult,
    GitCheckoutResult,
    GitCommitResult,
    GitDiffResult,
    GitLogResult,
    GitPushResult,
    GitResetResult,
    GitShowResult,
    GitStageResult,
    GitStashResult,
    GitStatusResult,
)
from limpiador.tools import git_tools
from limpiador.tools.registry import CORE_TOOL_NAMES, ToolRegistry

# The twelve tools this namespace is specified to expose (ARCHITECTURE.md §5.3).
_GIT_TOOL_NAMES = (
    "git.status",
    "git.diff",
    "git.log",
    "git.show",
    "git.branch_list",
    "git.branch_create",
    "git.checkout",
    "git.stage",
    "git.commit",
    "git.reset",
    "git.stash",
    "git.push",
    "git.blame",
)

_TOOLS_BY_NAME = {tool.name: tool for tool in git_tools.TOOLS}


def _tool(name: str):
    """The constructed tool instance for a ``git.<name>`` capability."""
    return _TOOLS_BY_NAME[name]


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """A real git repo with one commit, with the cwd moved inside it.

    The git tools resolve the repository ambiently from the working directory
    (the request schemas carry no repo path), so a test points a tool at a repo
    by ``chdir``-ing into it — exactly how the CLI anchors the agent to ``--repo``.
    """
    repo = Repo.init(tmp_path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Tester")
        writer.set_value("user", "email", "tester@example.com")
    (tmp_path / "a.txt").write_text("hello\n")
    repo.index.add(["a.txt"])
    repo.index.commit("initial commit")
    monkeypatch.chdir(tmp_path)
    return repo


@pytest.fixture
def empty_repo(tmp_path, monkeypatch):
    """An initialized repo with *no* commits, cwd moved inside it."""
    repo = Repo.init(tmp_path)
    monkeypatch.chdir(tmp_path)
    return repo


@pytest.fixture
def not_a_repo(tmp_path, monkeypatch):
    """A plain directory that is not a git repository, set as the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _repo_path(repo: Repo) -> pathlib.Path:
    return pathlib.Path(repo.working_tree_dir)


# ---- happy paths: each tool returns the right typed object -------------------
def test_status_reports_staged_unstaged_and_untracked(seeded_repo) -> None:
    root = _repo_path(seeded_repo)
    (root / "staged.txt").write_text("s\n")
    seeded_repo.index.add(["staged.txt"])
    (root / "a.txt").write_text("hello again\n")  # tracked -> unstaged
    (root / "untracked.txt").write_text("u\n")

    result = _tool("git.status").invoke({})

    assert isinstance(result, GitStatusResult)
    assert result.branch  # a concrete branch name, not blank
    assert "staged.txt" in result.staged
    assert "a.txt" in result.unstaged
    assert "untracked.txt" in result.untracked
    assert result.clean is False


def test_diff_shows_unstaged_changes_and_files(seeded_repo) -> None:
    (_repo_path(seeded_repo) / "a.txt").write_text("hello world\n")

    result = _tool("git.diff").invoke({})

    assert isinstance(result, GitDiffResult)
    assert "a.txt" in result.diff
    assert result.files_changed == ["a.txt"]


def test_diff_staged_shows_the_index(seeded_repo) -> None:
    root = _repo_path(seeded_repo)
    (root / "a.txt").write_text("staged change\n")
    seeded_repo.index.add(["a.txt"])

    result = _tool("git.diff").invoke({"staged": True})

    assert "staged change" in result.diff
    assert result.files_changed == ["a.txt"]


def test_log_returns_typed_commits(seeded_repo) -> None:
    result = _tool("git.log").invoke({})

    assert isinstance(result, GitLogResult)
    assert len(result.commits) == 1
    commit = result.commits[0]
    assert commit.message == "initial commit"
    assert commit.author == "Tester"
    assert len(commit.sha) == 40


def test_show_returns_commit_and_diff(seeded_repo) -> None:
    result = _tool("git.show").invoke({"ref": "HEAD"})

    assert isinstance(result, GitShowResult)
    assert result.commit.message == "initial commit"
    assert "a.txt" in result.diff


def test_branch_list_includes_current_and_new_branch(seeded_repo) -> None:
    seeded_repo.create_head("feature")

    result = _tool("git.branch_list").invoke({})

    assert isinstance(result, GitBranchListResult)
    assert "feature" in result.branches
    assert result.current in result.branches


def test_branch_create_makes_the_branch(seeded_repo) -> None:
    result = _tool("git.branch_create").invoke({"name": "feature"})

    assert isinstance(result, GitBranchCreateResult)
    assert result.name == "feature"
    assert result.created is True
    assert "feature" in [head.name for head in seeded_repo.heads]


def test_checkout_switches_branch_and_reports_previous(seeded_repo) -> None:
    start = seeded_repo.active_branch.name
    seeded_repo.create_head("dev")

    result = _tool("git.checkout").invoke({"ref": "dev"})

    assert isinstance(result, GitCheckoutResult)
    assert result.ref == "dev"
    assert result.previous == start
    assert seeded_repo.active_branch.name == "dev"


def test_checkout_create_makes_and_switches(seeded_repo) -> None:
    result = _tool("git.checkout").invoke({"ref": "brand-new", "create": True})

    assert result.ref == "brand-new"
    assert seeded_repo.active_branch.name == "brand-new"


def test_checkout_can_create_a_branch_from_a_base_ref(seeded_repo) -> None:
    base_sha = seeded_repo.head.commit.hexsha
    (_repo_path(seeded_repo) / "later.txt").write_text("later\n")
    seeded_repo.index.add(["later.txt"])
    seeded_repo.index.commit("later commit")

    result = _tool("git.checkout").invoke(
        {"ref": "from-base", "create": True, "base": base_sha}
    )

    assert result.ref == "from-base"
    assert seeded_repo.active_branch.name == "from-base"
    assert seeded_repo.head.commit.hexsha == base_sha


def test_stage_adds_paths_to_the_index(seeded_repo) -> None:
    (_repo_path(seeded_repo) / "new.txt").write_text("new\n")

    result = _tool("git.stage").invoke({"paths": ["new.txt"]})

    assert isinstance(result, GitStageResult)
    assert "new.txt" in result.staged
    assert "new.txt" in {key[0] for key in seeded_repo.index.entries}


def test_commit_records_staged_changes(seeded_repo) -> None:
    seeded_repo.git.checkout("-b", "feature")
    (_repo_path(seeded_repo) / "new.txt").write_text("new\n")
    seeded_repo.index.add(["new.txt"])

    result = _tool("git.commit").invoke({"message": "add new"})

    assert isinstance(result, GitCommitResult)
    assert result.message == "add new"
    assert len(result.sha) == 40
    assert seeded_repo.head.commit.message.strip() == "add new"


def test_commit_on_a_protected_branch_raises_malformed_input(seeded_repo) -> None:
    seeded_repo.git.branch("-M", "main")
    (_repo_path(seeded_repo) / "new.txt").write_text("new\n")
    seeded_repo.index.add(["new.txt"])

    with pytest.raises(MalformedInputError):
        _tool("git.commit").invoke({"message": "add new"})

    assert seeded_repo.head.commit.message.strip() == "initial commit"


def test_reset_unstages_changes(seeded_repo) -> None:
    (_repo_path(seeded_repo) / "new.txt").write_text("new\n")
    seeded_repo.index.add(["new.txt"])

    result = _tool("git.reset").invoke({})

    assert isinstance(result, GitResetResult)
    assert result.ref == "HEAD"
    # mixed reset to HEAD leaves the file present but no longer staged
    assert "new.txt" not in {
        diff.a_path for diff in seeded_repo.index.diff(seeded_repo.head.commit)
    }


def test_stash_saves_and_cleans_the_working_tree(seeded_repo) -> None:
    (_repo_path(seeded_repo) / "a.txt").write_text("dirty\n")

    result = _tool("git.stash").invoke({})

    assert isinstance(result, GitStashResult)
    assert result.popped is False
    assert result.stash_ref is not None
    assert not seeded_repo.is_dirty()


def test_stash_pop_restores_changes(seeded_repo) -> None:
    (_repo_path(seeded_repo) / "a.txt").write_text("dirty\n")
    _tool("git.stash").invoke({})

    result = _tool("git.stash").invoke({"pop": True})

    assert result.popped is True
    assert seeded_repo.is_dirty()


def test_blame_attributes_lines_to_commits(seeded_repo) -> None:
    result = _tool("git.blame").invoke({"file": "a.txt"})

    assert isinstance(result, GitBlameResult)
    assert result.file == "a.txt"
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.line == 1
    assert line.author == "Tester"
    assert line.content == "hello"
    assert line.sha == seeded_repo.head.commit.hexsha


def test_blame_honors_a_line_range(seeded_repo) -> None:
    root = _repo_path(seeded_repo)
    root.joinpath("a.txt").write_text("one\ntwo\nthree\n")
    seeded_repo.index.add(["a.txt"])
    seeded_repo.index.commit("three lines")

    result = _tool("git.blame").invoke({"file": "a.txt", "line_start": 2, "line_end": 3})

    assert [line.line for line in result.lines] == [2, 3]
    assert [line.content for line in result.lines] == ["two", "three"]


def test_push_sends_the_branch_to_a_remote(seeded_repo) -> None:
    # A bare repo stands in for the remote (no network); the seeded repo pushes
    # its branch to it and the remote ends up pointing at the same commit.
    seeded_repo.git.checkout("-b", "feature")
    remote_path = _repo_path(seeded_repo) / "remote.git"
    Repo.init(remote_path, bare=True)
    seeded_repo.create_remote("origin", str(remote_path))
    branch = seeded_repo.active_branch.name

    result = _tool("git.push").invoke(
        {"remote": "origin", "branch": branch, "set_upstream": True}
    )

    assert isinstance(result, GitPushResult)
    assert result.remote == "origin"
    assert result.branch == branch
    assert result.pushed is True
    remote = Repo(remote_path)
    assert branch in [head.name for head in remote.heads]
    assert remote.heads[branch].commit.hexsha == seeded_repo.head.commit.hexsha


def test_push_defaults_to_the_current_branch(seeded_repo) -> None:
    seeded_repo.git.checkout("-b", "feature")
    remote_path = _repo_path(seeded_repo) / "remote.git"
    Repo.init(remote_path, bare=True)
    seeded_repo.create_remote("origin", str(remote_path))

    result = _tool("git.push").invoke({"remote": "origin", "set_upstream": True})

    assert result.branch == seeded_repo.active_branch.name


def test_push_on_a_protected_branch_raises_malformed_input(seeded_repo) -> None:
    seeded_repo.git.branch("-M", "main")
    remote_path = _repo_path(seeded_repo) / "remote.git"
    Repo.init(remote_path, bare=True)
    seeded_repo.create_remote("origin", str(remote_path))

    with pytest.raises(MalformedInputError):
        _tool("git.push").invoke({"remote": "origin", "branch": "main"})

    remote = Repo(remote_path)
    assert "main" not in [head.name for head in remote.heads]


def test_push_refspec_to_a_protected_branch_raises_malformed_input(seeded_repo) -> None:
    seeded_repo.git.checkout("-b", "feature")
    remote_path = _repo_path(seeded_repo) / "remote.git"
    Repo.init(remote_path, bare=True)
    seeded_repo.create_remote("origin", str(remote_path))

    with pytest.raises(MalformedInputError):
        _tool("git.push").invoke({"remote": "origin", "branch": "feature:main"})

    remote = Repo(remote_path)
    assert "main" not in [head.name for head in remote.heads]


# ---- failure paths: each tool raises the correct typed error -----------------
def test_status_outside_a_repo_raises_not_found(not_a_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.status").invoke({})


def test_branch_list_outside_a_repo_raises_not_found(not_a_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.branch_list").invoke({})


def test_diff_with_unknown_ref_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.diff").invoke({"ref": "no-such-ref"})


def test_log_with_no_commits_raises_not_found(empty_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.log").invoke({})


def test_show_with_unknown_ref_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.show").invoke({"ref": "deadbeef"})


def test_branch_create_with_unknown_base_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.branch_create").invoke({"name": "x", "base": "no-such-base"})


def test_checkout_unknown_ref_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.checkout").invoke({"ref": "no-such-ref"})


def test_stage_missing_path_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.stage").invoke({"paths": ["does-not-exist.txt"]})


def test_commit_with_nothing_staged_raises_malformed_input(seeded_repo) -> None:
    with pytest.raises(MalformedInputError):
        _tool("git.commit").invoke({"message": "nothing here"})


def test_reset_unknown_ref_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.reset").invoke({"ref": "no-such-ref"})


def test_stash_pop_with_no_stash_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.stash").invoke({"pop": True})


def test_blame_missing_file_raises_not_found(seeded_repo) -> None:
    with pytest.raises(NotFoundError):
        _tool("git.blame").invoke({"file": "missing.py"})


def test_push_to_an_unknown_remote_raises_not_found(seeded_repo) -> None:
    seeded_repo.git.checkout("-b", "feature")
    with pytest.raises(NotFoundError):
        _tool("git.push").invoke({"remote": "no-such-remote"})


def test_every_failure_is_a_recoverable_tool_error(not_a_repo) -> None:
    # A failing git tool surfaces a ToolError the loop can fold back into context,
    # not a raw gitpython exception that would crash the run.
    with pytest.raises(ToolError):
        _tool("git.status").invoke({})


# ---- registry: all twelve searchable + loadable, none in the core -----------
def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in git_tools.TOOLS:
        registry.register(tool)
    return registry


def test_namespace_exposes_exactly_the_thirteen_tools() -> None:
    assert tuple(tool.name for tool in git_tools.TOOLS) == _GIT_TOOL_NAMES


def test_every_git_tool_is_loadable_and_none_are_core() -> None:
    registry = _fresh_registry()
    for name in _GIT_TOOL_NAMES:
        assert name not in CORE_TOOL_NAMES
        assert registry.load({"name": name}).loaded is True
    assert set(registry.loaded_names()) == set(_GIT_TOOL_NAMES)


def test_every_git_tool_is_searchable() -> None:
    registry = _fresh_registry()
    for name in _GIT_TOOL_NAMES:
        verb = name.split(".", 1)[1].replace("_", " ")
        found = registry.search({"query": verb, "limit": 56}).summaries
        assert name in {summary.name for summary in found}


def test_active_schemas_are_core_only_until_a_git_tool_is_loaded() -> None:
    registry = _fresh_registry()
    assert len(registry.active_schemas()) == len(CORE_TOOL_NAMES)
    registry.load({"name": "git.status"})
    assert len(registry.active_schemas()) == len(CORE_TOOL_NAMES) + 1


# ---- CLEAN_CODE: every function in the module is single-purpose and small ----
_MAX_FUNCTION_LINES = 60


def test_every_function_stays_under_the_size_budget() -> None:
    source = pathlib.Path(git_tools.__file__).read_text()
    tree = ast.parse(source)
    oversized = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span >= _MAX_FUNCTION_LINES:
                oversized.append((node.name, span))
    assert oversized == [], f"functions over {_MAX_FUNCTION_LINES} lines: {oversized}"

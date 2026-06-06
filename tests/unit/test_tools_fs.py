"""Unit tests for the ``fs.*`` namespace — scoped filesystem access (HAR-19).

The filesystem tools are the agent's hands on the working tree, so two
properties matter beyond "does it read and write": their outputs stay *small
and scoped* (matching lines and line slices, never whole files dumped into
context), and they cannot *escape the repo root* — a ``../../etc`` path is a
typed ``PermissionDeniedError``, not a silent read of the host. ``apply_patch``
is held to being *atomic*: a patch with one bad hunk changes nothing on disk.

Like the git tools, the fs tools resolve their boundary ambiently from the
current working directory (the ``--repo`` the CLI anchors the agent to), so a
test points them at a tree by ``chdir``-ing into a ``tmp_path``. Each tool is
held to the base contract — the right *typed* object on success, the *correct
typed* ``ToolError`` on failure — and all ten are pinned into the registry as
searchable, loadable, and absent from the fixed core.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from limpiador.observability.errors import (
    NotFoundError,
    PermissionDeniedError,
    ToolError,
)
from limpiador.schemas import (
    FsApplyPatchResult,
    FsDeleteResult,
    FsDirListing,
    FsFileContent,
    FsFileStat,
    FsGlobResult,
    FsGrepResult,
    FsMkdirResult,
    FsMoveResult,
    FsWriteResult,
)
from limpiador.tools import fs_tools
from limpiador.tools.registry import CORE_TOOL_NAMES, ToolRegistry

# The ten tools this namespace is specified to expose (ARCHITECTURE.md §5.3).
_FS_TOOL_NAMES = (
    "fs.read_file",
    "fs.write_file",
    "fs.list_dir",
    "fs.glob",
    "fs.grep",
    "fs.move",
    "fs.delete",
    "fs.mkdir",
    "fs.file_stat",
    "fs.apply_patch",
)

_TOOLS_BY_NAME = {tool.name: tool for tool in fs_tools.TOOLS}


def _tool(name: str):
    """The constructed tool instance for an ``fs.<name>`` capability."""
    return _TOOLS_BY_NAME[name]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A small working tree with the cwd moved inside it.

    The fs tools resolve their root ambiently from the working directory, so a
    test anchors them by ``chdir``-ing into the tree — exactly how the CLI
    anchors the agent to ``--repo``.
    """
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\ndelta\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def total(a, b):\n    return a + b\n")
    (tmp_path / "src" / "util.py").write_text("VALUE = 1\n# needle here\nVALUE = 2\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---- happy paths: each tool returns the right typed object -------------------
def test_read_file_returns_content_and_line_count(tree) -> None:
    result = _tool("fs.read_file").invoke({"path": "a.txt"})

    assert isinstance(result, FsFileContent)
    assert result.content == "alpha\nbeta\ngamma\ndelta\n"
    assert result.line_count == 4
    assert result.start_line == 1


def test_read_file_honors_a_line_range(tree) -> None:
    result = _tool("fs.read_file").invoke(
        {"path": "a.txt", "start_line": 2, "end_line": 3}
    )

    # Only the requested slice comes back — not the whole file.
    assert result.content == "beta\ngamma\n"
    assert result.line_count == 2
    assert result.start_line == 2


def test_write_file_creates_and_reports_bytes(tree) -> None:
    result = _tool("fs.write_file").invoke({"path": "new.txt", "content": "hi\n"})

    assert isinstance(result, FsWriteResult)
    assert result.path == "new.txt"
    assert result.bytes_written == 3
    assert (tree / "new.txt").read_text() == "hi\n"


def test_write_file_creates_missing_parent_dirs(tree) -> None:
    _tool("fs.write_file").invoke({"path": "deep/nested/file.txt", "content": "x"})

    assert (tree / "deep" / "nested" / "file.txt").read_text() == "x"


def test_list_dir_reports_entries_and_kinds(tree) -> None:
    result = _tool("fs.list_dir").invoke({"path": "."})

    assert isinstance(result, FsDirListing)
    by_name = {entry.name: entry.is_dir for entry in result.entries}
    assert by_name["a.txt"] is False
    assert by_name["src"] is True


def test_glob_finds_matching_files(tree) -> None:
    result = _tool("fs.glob").invoke({"pattern": "src/*.py"})

    assert isinstance(result, FsGlobResult)
    assert set(result.matches) == {"src/calc.py", "src/util.py"}


def test_grep_returns_matching_lines_not_whole_files(tree) -> None:
    result = _tool("fs.grep").invoke({"pattern": "needle", "path": "src"})

    assert isinstance(result, FsGrepResult)
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.file == "src/util.py"
    assert match.line == 2
    assert match.text == "# needle here"
    # The match carries one line, never the whole file's body.
    assert "\n" not in match.text


def test_grep_literal_mode_does_not_treat_pattern_as_regex(tree) -> None:
    (tree / "lit.txt").write_text("a+b is the answer\n")

    result = _tool("fs.grep").invoke({"pattern": "a+b", "path": "lit.txt", "regex": False})

    assert [m.text for m in result.matches] == ["a+b is the answer"]


def test_move_renames_a_file(tree) -> None:
    result = _tool("fs.move").invoke({"source": "a.txt", "destination": "b.txt"})

    assert isinstance(result, FsMoveResult)
    assert not (tree / "a.txt").exists()
    assert (tree / "b.txt").exists()


def test_delete_removes_a_file(tree) -> None:
    result = _tool("fs.delete").invoke({"path": "a.txt"})

    assert isinstance(result, FsDeleteResult)
    assert result.deleted is True
    assert not (tree / "a.txt").exists()


def test_delete_removes_a_directory_tree(tree) -> None:
    _tool("fs.delete").invoke({"path": "src"})

    assert not (tree / "src").exists()


def test_mkdir_creates_nested_directories(tree) -> None:
    result = _tool("fs.mkdir").invoke({"path": "x/y/z"})

    assert isinstance(result, FsMkdirResult)
    assert result.created is True
    assert (tree / "x" / "y" / "z").is_dir()


def test_file_stat_reports_size_and_kind(tree) -> None:
    result = _tool("fs.file_stat").invoke({"path": "a.txt"})

    assert isinstance(result, FsFileStat)
    assert result.exists is True
    assert result.is_dir is False
    assert result.size_bytes == len("alpha\nbeta\ngamma\ndelta\n")


def test_file_stat_reports_a_missing_path_as_absent(tree) -> None:
    # file_stat is a query, not an assertion — a missing path is exists=False,
    # not a raised error.
    result = _tool("fs.file_stat").invoke({"path": "ghost.txt"})

    assert result.exists is False


def test_apply_patch_modifies_a_file(tree) -> None:
    patch = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def total(a, b):\n"
        "-    return a + b\n"
        "+    return a - b\n"
    )

    result = _tool("fs.apply_patch").invoke({"patch": patch})

    assert isinstance(result, FsApplyPatchResult)
    assert result.applied is True
    assert "src/calc.py" in result.files_changed
    assert (tree / "src" / "calc.py").read_text() == "def total(a, b):\n    return a - b\n"


# ---- failure paths: scoped, typed, and atomic --------------------------------
@pytest.mark.parametrize("name", ["fs.read_file", "fs.delete", "fs.file_stat"])
def test_a_path_escape_attempt_is_denied(tree, name) -> None:
    # ../../etc must never reach outside the repo root, on any tool.
    with pytest.raises(PermissionDeniedError):
        _tool(name).invoke({"path": "../../etc/passwd"})


def test_write_outside_the_root_is_denied(tree) -> None:
    with pytest.raises(PermissionDeniedError):
        _tool("fs.write_file").invoke({"path": "../escape.txt", "content": "x"})


def test_an_absolute_path_outside_the_root_is_denied(tree) -> None:
    with pytest.raises(PermissionDeniedError):
        _tool("fs.read_file").invoke({"path": "/etc/passwd"})


def test_read_file_missing_raises_not_found(tree) -> None:
    with pytest.raises(NotFoundError):
        _tool("fs.read_file").invoke({"path": "nope.txt"})


def test_apply_patch_rolls_back_cleanly_on_a_bad_hunk(tree) -> None:
    original = (tree / "src" / "calc.py").read_text()
    bad_patch = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def DOES_NOT_MATCH(a, b):\n"
        "-    return a + b\n"
        "+    return a - b\n"
    )

    with pytest.raises(ToolError):
        _tool("fs.apply_patch").invoke({"patch": bad_patch})

    # Atomic: the failed hunk left the file exactly as it was.
    assert (tree / "src" / "calc.py").read_text() == original


def test_apply_patch_is_atomic_across_multiple_files(tree) -> None:
    # A two-file patch whose second file's hunk is bad must change neither file.
    before_calc = (tree / "src" / "calc.py").read_text()
    before_util = (tree / "src" / "util.py").read_text()
    patch = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def total(a, b):\n"
        "-    return a + b\n"
        "+    return a - b\n"
        "--- a/src/util.py\n"
        "+++ b/src/util.py\n"
        "@@ -1,1 +1,1 @@\n"
        " WRONG CONTEXT\n"
        "+inserted\n"
    )

    with pytest.raises(ToolError):
        _tool("fs.apply_patch").invoke({"patch": patch})

    assert (tree / "src" / "calc.py").read_text() == before_calc
    assert (tree / "src" / "util.py").read_text() == before_util


# ---- registry: all ten searchable + loadable, none in the core ---------------
def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in fs_tools.TOOLS:
        registry.register(tool)
    return registry


def test_namespace_exposes_exactly_the_ten_tools() -> None:
    assert tuple(tool.name for tool in fs_tools.TOOLS) == _FS_TOOL_NAMES


def test_every_fs_tool_is_loadable_and_none_are_core() -> None:
    registry = _fresh_registry()
    for name in _FS_TOOL_NAMES:
        assert name not in CORE_TOOL_NAMES
        assert registry.load({"name": name}).loaded is True
    assert set(registry.loaded_names()) == set(_FS_TOOL_NAMES)


def test_every_fs_tool_is_searchable() -> None:
    registry = _fresh_registry()
    for name in _FS_TOOL_NAMES:
        verb = name.split(".", 1)[1].replace("_", " ")
        found = registry.search({"query": verb, "limit": 56}).summaries
        assert name in {summary.name for summary in found}


# ---- CLEAN_CODE: every function in the module is single-purpose and small ----
_MAX_FUNCTION_LINES = 60


def test_every_function_stays_under_the_size_budget() -> None:
    source = pathlib.Path(fs_tools.__file__).read_text()
    tree_ = ast.parse(source)
    oversized = []
    for node in ast.walk(tree_):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span >= _MAX_FUNCTION_LINES:
                oversized.append((node.name, span))
    assert oversized == [], f"functions over {_MAX_FUNCTION_LINES} lines: {oversized}"

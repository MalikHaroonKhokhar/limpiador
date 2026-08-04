"""Unit tests for the ``ast.*`` namespace — semantic code understanding (HAR-20).

This is the differentiator (ARCHITECTURE.md §5.3): limpiador resolves *symbols*
against a real parse tree (tree-sitter), it does not grep for text. The tests
pin that distinction directly — a symbol named in a comment or a string literal
is **not** a reference, because it is not an identifier node — and they assert
defs / refs / imports / cycles against a known source tree *exactly*, not
fuzzily.

The namespace also anchors Property #5 (typed composability): ``find_references``
returns a :class:`RefList`, and that *same object* is handed to
``rename_symbol`` with no re-parsing and no string passing in between. The rename
then edits exactly the resolved sites and nothing else — the comment and string
that merely *contain* the old name are left untouched.
"""

from __future__ import annotations

import ast as py_ast
import pathlib

import pytest

from limpiador.observability.errors import MalformedInputError, NotFoundError
from limpiador.schemas import (
    AstCallGraph,
    AstComplexityResult,
    AstCyclesResult,
    AstDeadCodeResult,
    AstDefinition,
    AstDependencyTree,
    AstExtractFunctionResult,
    AstImportList,
    AstParseResult,
    AstRenameResult,
    AstSymbolList,
    Reference,
    RefList,
    RenameSymbolRequest,
)
from limpiador.tools import ast_tools
from limpiador.tools.registry import CORE_TOOL_NAMES, ToolRegistry

# The twelve tools this namespace is specified to expose (ARCHITECTURE.md §5.3).
_AST_TOOL_NAMES = (
    "ast.parse_file",
    "ast.list_symbols",
    "ast.find_definition",
    "ast.find_references",
    "ast.call_graph",
    "ast.dependency_tree",
    "ast.find_dead_code",
    "ast.detect_cycles",
    "ast.rename_symbol",
    "ast.extract_function",
    "ast.list_imports",
    "ast.complexity_score",
)

_TOOLS_BY_NAME = {tool.name: tool for tool in ast_tools.TOOLS}


def _tool(name: str):
    return _TOOLS_BY_NAME[name]


# A known source tree. Every assertion below is checked against these exact
# files, so the symbol/line/import expectations are concrete, not fuzzy.
_CORE = '''from pkg.helpers import helper

CONSTANT = 10


def compute(value):
    # compute the total
    label = "compute"
    total = helper(value)
    return total + CONSTANT


def main():
    return compute(5)


class Engine:
    def run(self):
        return compute(1)
'''

_HELPERS = '''def helper(x):
    if x > 0:
        return x * 2
    return 0


def unused_helper(y):
    return y
'''

_CYCLE_A = '''from pkg.cycle_b import beta


def alpha():
    return beta()
'''

_CYCLE_B = '''from pkg.cycle_a import alpha


def beta():
    return alpha()
'''

_EXTRACTME = '''def routine():
    a = 1
    b = 2
    return a + b
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small ``pkg`` package with the cwd moved inside the repo root."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(_CORE)
    (pkg / "helpers.py").write_text(_HELPERS)
    (pkg / "cycle_a.py").write_text(_CYCLE_A)
    (pkg / "cycle_b.py").write_text(_CYCLE_B)
    (pkg / "extractme.py").write_text(_EXTRACTME)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---- parse_file -------------------------------------------------------------
def test_parse_file_reports_a_parsed_python_tree(project) -> None:
    result = _tool("ast.parse_file").invoke({"file": "pkg/core.py"})

    assert isinstance(result, AstParseResult)
    assert result.language == "python"
    assert result.ok is True
    assert result.node_count > 0


def test_parse_file_missing_raises_not_found(project) -> None:
    with pytest.raises(NotFoundError):
        _tool("ast.parse_file").invoke({"file": "pkg/ghost.py"})


# ---- list_symbols: exact symbols, kinds, and lines --------------------------
def test_list_symbols_matches_the_known_definitions_exactly(project) -> None:
    result = _tool("ast.list_symbols").invoke({"file": "pkg/core.py"})

    assert isinstance(result, AstSymbolList)
    found = {(s.name, s.kind, s.line) for s in result.symbols}
    assert found == {
        ("CONSTANT", "variable", 3),
        ("compute", "function", 6),
        ("main", "function", 13),
        ("Engine", "class", 17),
        ("run", "method", 18),
    }


# ---- find_definition --------------------------------------------------------
def test_find_definition_resolves_the_symbol(project) -> None:
    result = _tool("ast.find_definition").invoke({"symbol": "compute"})

    assert isinstance(result, AstDefinition)
    assert result.file == "pkg/core.py"
    assert result.line == 6
    assert result.kind == "function"


def test_find_definition_unknown_symbol_raises_not_found(project) -> None:
    with pytest.raises(NotFoundError):
        _tool("ast.find_definition").invoke({"symbol": "does_not_exist"})


# ---- find_references: identifiers, never strings or comments -----------------
def test_find_references_resolves_identifier_sites_not_text(project) -> None:
    result = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})

    assert isinstance(result, RefList)
    assert result.symbol == "compute"
    lines = sorted(ref.line for ref in result.references)
    # The definition (6) and the two call sites (14, 19) — NOT the comment on
    # line 7 nor the string literal on line 8. That is the grep/AST distinction.
    assert lines == [6, 14, 19]
    assert all(ref.file == "pkg/core.py" for ref in result.references)


# ---- composability: find_references output feeds rename_symbol --------------
def test_find_references_output_validates_and_feeds_rename(project) -> None:
    refs = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})
    assert isinstance(refs, RefList)  # the typed handoff object

    # The whole RefList object is handed across unchanged — no re-parse, no
    # string passing. This is the Property #5 contract made concrete.
    request = RenameSymbolRequest(references=refs, new_name="calculate")
    result = _tool("ast.rename_symbol").invoke(request)

    assert isinstance(result, AstRenameResult)
    assert result.new_name == "calculate"
    assert result.sites_changed == 3
    assert result.files_changed == ["pkg/core.py"]

    text = (project / "pkg" / "core.py").read_text()
    # All sites updated...
    assert "def calculate(value):" in text
    assert "return calculate(5)" in text
    assert "return calculate(1)" in text
    # ...and none beyond: the old identifier as a definition/call is gone, but
    # the comment and the string literal that merely contain "compute" remain.
    assert "def compute(" not in text
    assert "# compute the total" in text
    assert 'label = "compute"' in text


def test_rename_rejects_a_reference_line_past_the_end_of_the_file(project) -> None:
    """A stale or hand-built reference must fail *recoverably*, not crash the run.

    The agent can hand over a RefList whose line numbers no longer match the file
    (it edited in between, or synthesised the list itself). Indexing blindly threw
    IndexError, which is not a ToolError — so it escaped the loop's folding and
    killed the whole session instead of letting the model adapt.
    """
    refs = RefList(
        symbol="compute",
        references=[Reference(file="pkg/core.py", line=9999, symbol="compute")],
    )
    with pytest.raises(MalformedInputError):
        _tool("ast.rename_symbol").invoke(RenameSymbolRequest(references=refs, new_name="calculate"))


def test_rename_changes_no_sites_outside_the_reference_list(project) -> None:
    refs = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})
    _tool("ast.rename_symbol").invoke(RenameSymbolRequest(references=refs, new_name="calculate"))

    # An unrelated file is never touched by a scoped rename.
    assert (project / "pkg" / "helpers.py").read_text() == _HELPERS


# ---- unresolved mentions: the dynamic-dispatch safety net --------------------
# A static rename can only see identifier nodes. Anything resolved at runtime — a
# reflective getattr, a string-keyed dispatch table, config that names a function
# — appears only in a string or comment, so the rename cannot touch it and would
# otherwise report a clean success while a dynamic call site still names the old
# symbol. find_references/rename now *flag* those sites (never edit them) so a
# "safe" rename cannot silently leave a dynamic reference dangling.
def test_find_references_flags_string_and_comment_mentions_as_unresolved(project) -> None:
    result = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})

    # The resolved references are unchanged — strings/comments are NOT references.
    assert sorted(ref.line for ref in result.references) == [6, 14, 19]
    # ...but they now surface as unresolved candidates to verify by hand: the
    # comment on line 7 and the string literal on line 8.
    by_line = {m.line: m for m in result.unresolved}
    assert set(by_line) == {7, 8}
    assert by_line[7].kind == "comment" and "compute the total" in by_line[7].context
    assert by_line[8].kind == "string" and 'label = "compute"' in by_line[8].context
    assert all(m.file == "pkg/core.py" for m in result.unresolved)


def test_rename_reports_the_string_and_comment_sites_it_left_alone(project) -> None:
    refs = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})
    result = _tool("ast.rename_symbol").invoke(RenameSymbolRequest(references=refs, new_name="calculate"))

    # The rename still edits exactly the resolved sites...
    assert result.sites_changed == 3
    text = (project / "pkg" / "core.py").read_text()
    assert "# compute the total" in text and 'label = "compute"' in text  # left intact
    # ...and the result now carries the flagged, un-edited sites still naming the
    # old symbol, so the success report cannot be mistaken for "fully renamed".
    flagged = {(m.kind, m.line) for m in result.unresolved}
    assert flagged == {("comment", 7), ("string", 8)}


def test_unresolved_mentions_respect_word_boundaries(project) -> None:
    """A candidate is a whole-word match, so a reflective ``getattr(obj, "compute")``
    is flagged but an unrelated ``"recompute_all"`` that merely contains the name
    is not — the flag is a signal, not indiscriminate substring noise."""
    (project / "pkg" / "dyn.py").write_text(
        'def wire(obj):\n'
        '    fn = getattr(obj, "compute")\n'
        '    other = "recompute_all"\n'
        '    return fn, other\n'
    )
    result = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})

    dyn = [m for m in result.unresolved if m.file == "pkg/dyn.py"]
    assert [m.line for m in dyn] == [2]  # the getattr string, and only it
    assert dyn[0].kind == "string" and "getattr" in dyn[0].context
    assert all("recompute_all" not in m.context for m in result.unresolved)


def test_fstring_interpolation_is_a_reference_not_an_unresolved_string(project) -> None:
    """An f-string that *calls* the symbol interpolates a real identifier reference
    — the rename edits it — so it must not ALSO be flagged as an un-edited string,
    or the flag contradicts its own contract on the most common dynamic-looking
    code. Only the literal text (string_content) counts, never the {interpolation}."""
    (project / "pkg" / "fstr.py").write_text(
        'def show(x):\n'
        '    return f"total is {compute(x)}"\n'
    )
    result = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})

    # The interpolated call is a resolved reference...
    assert ("pkg/fstr.py", 2) in {(r.file, r.line) for r in result.references}
    # ...and it is NOT reported as an unresolved string site.
    assert all(m.file != "pkg/fstr.py" for m in result.unresolved)


def test_a_string_key_inside_an_interpolation_is_still_flagged(project) -> None:
    """The literal text of a nested string inside an interpolation is still a
    genuine dynamic-dispatch site — ``f"{d['compute']}"`` — so it stays flagged,
    exactly once (the outer f-string wrapper must not double-count it)."""
    (project / "pkg" / "dyn2.py").write_text(
        'def wire(d):\n'
        "    return f\"{d['compute']}\"\n"
    )
    result = _tool("ast.find_references").invoke({"file": "pkg/core.py", "symbol": "compute"})

    dyn = [m for m in result.unresolved if m.file == "pkg/dyn2.py"]
    assert [m.line for m in dyn] == [2]
    assert dyn[0].kind == "string"


# ---- call_graph -------------------------------------------------------------
def test_call_graph_traverses_real_calls(project) -> None:
    result = _tool("ast.call_graph").invoke({"symbol": "main", "depth": 2})

    assert isinstance(result, AstCallGraph)
    assert result.root == "main"
    edges = {(e.caller, e.callee) for e in result.edges}
    assert ("main", "compute") in edges
    assert ("compute", "helper") in edges


# ---- dependency_tree --------------------------------------------------------
def test_dependency_tree_follows_local_imports(project) -> None:
    result = _tool("ast.dependency_tree").invoke({"file": "pkg/core.py"})

    assert isinstance(result, AstDependencyTree)
    assert result.root == "pkg.core"
    edges = {(e.module, e.imports) for e in result.edges}
    assert ("pkg.core", "pkg.helpers") in edges


# ---- find_dead_code ---------------------------------------------------------
def test_find_dead_code_finds_the_unreferenced_symbol(project) -> None:
    result = _tool("ast.find_dead_code").invoke({"path": "pkg"})

    assert isinstance(result, AstDeadCodeResult)
    dead = {s.name for s in result.symbols}
    assert "unused_helper" in dead
    # Referenced symbols are not dead.
    assert "helper" not in dead
    assert "compute" not in dead


# ---- detect_cycles: a planted import cycle ----------------------------------
def test_detect_cycles_finds_the_planted_cycle(project) -> None:
    result = _tool("ast.detect_cycles").invoke({"path": "pkg"})

    assert isinstance(result, AstCyclesResult)
    cycle_sets = [set(c.modules) for c in result.cycles]
    assert {"pkg.cycle_a", "pkg.cycle_b"} in cycle_sets


# ---- list_imports: exact module + names -------------------------------------
def test_list_imports_matches_exactly(project) -> None:
    result = _tool("ast.list_imports").invoke({"file": "pkg/core.py"})

    assert isinstance(result, AstImportList)
    assert len(result.imports) == 1
    only = result.imports[0]
    assert only.module == "pkg.helpers"
    assert only.names == ["helper"]
    assert only.line == 1


# ---- complexity_score -------------------------------------------------------
def test_complexity_score_counts_branches_per_symbol(project) -> None:
    result = _tool("ast.complexity_score").invoke({"file": "pkg/helpers.py"})

    assert isinstance(result, AstComplexityResult)
    per = {s.symbol: s.score for s in result.per_symbol}
    assert per["helper"] == 2  # base 1 + one `if`
    assert per["unused_helper"] == 1  # no branches
    assert result.score == 3


# ---- extract_function -------------------------------------------------------
def test_extract_function_creates_a_new_function(project) -> None:
    result = _tool("ast.extract_function").invoke(
        {"file": "pkg/extractme.py", "line_start": 2, "line_end": 3, "name": "setup"}
    )

    assert isinstance(result, AstExtractFunctionResult)
    assert result.function_name == "setup"
    text = (project / "pkg" / "extractme.py").read_text()
    assert "def setup(" in text
    # The new file still parses as valid Python.
    py_ast.parse(text)


# ---- registry: all twelve searchable + loadable, none in the core -----------
def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in ast_tools.TOOLS:
        registry.register(tool)
    return registry


def test_namespace_exposes_exactly_the_twelve_tools() -> None:
    assert tuple(tool.name for tool in ast_tools.TOOLS) == _AST_TOOL_NAMES


def test_every_ast_tool_is_loadable_and_none_are_core() -> None:
    registry = _fresh_registry()
    for name in _AST_TOOL_NAMES:
        assert name not in CORE_TOOL_NAMES
        assert registry.load({"name": name}).loaded is True
    assert set(registry.loaded_names()) == set(_AST_TOOL_NAMES)


def test_every_ast_tool_is_searchable() -> None:
    registry = _fresh_registry()
    for name in _AST_TOOL_NAMES:
        verb = name.split(".", 1)[1].replace("_", " ")
        found = registry.search({"query": verb, "limit": 56}).summaries
        assert name in {summary.name for summary in found}


# ---- CLEAN_CODE: every function in the module is single-purpose and small ----
_MAX_FUNCTION_LINES = 60


def test_every_function_stays_under_the_size_budget() -> None:
    source = pathlib.Path(ast_tools.__file__).read_text()
    tree = py_ast.parse(source)
    oversized = []
    for node in py_ast.walk(tree):
        if isinstance(node, (py_ast.FunctionDef, py_ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span >= _MAX_FUNCTION_LINES:
                oversized.append((node.name, span))
    assert oversized == [], f"functions over {_MAX_FUNCTION_LINES} lines: {oversized}"

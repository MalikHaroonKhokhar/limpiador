"""Unit tests for the tool registry — property #1 (ARCHITECTURE.md §5, .clauderules §5).

The registry is the hard part of the build: it keeps 50+ tools coherent through
*model-driven discovery* rather than a 50-branch dispatch. The model always sees
only a tiny fixed core (``search_tools`` / ``load_tool`` / ``finish``) and must
discover and load everything else. These tests pin that contract down:

* the core is always present; the full menu never is;
* ``search_tools`` returns ranked one-line summaries — never full schemas;
* ``load_tool`` promotes exactly one tool into the active set; unloaded tools
  never leak into ``active_schemas()``;
* a brand-new tool the registry has never heard of is fully usable with **zero**
  registry edits (open/closed — the anti-50-branch proof);
* edges (empty query, unknown name, malformed input) are typed, not crashes;
* re-searching emits the ``[REGISTRY RESEARCH_RETRY]`` debt tag (ARCH_DEBT_001).
"""

from __future__ import annotations

import pathlib

import pytest

from limpiador.observability.errors import MalformedInputError, NotFoundError
from limpiador.observability.tracing import RESEARCH_RETRY_TAG
from limpiador.schemas import Schema, ToolSummary
from limpiador.tools.base import Tool
from limpiador.tools.registry import (
    CORE_TOOL_NAMES,
    FINISH,
    LOAD_TOOL,
    PLAN_ADD,
    PLAN_RESOLVE,
    REGISTRY,
    SEARCH_TOOLS,
    ToolRegistry,
)

_REGISTRY_SRC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "limpiador"
    / "tools"
    / "registry.py"
)

_DECLARED_TOOL_NAMES = {
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
    "github.get_issue",
    "github.list_issues",
    "github.create_issue",
    "github.comment_issue",
    "github.get_pr",
    "github.list_prs",
    "github.create_pr",
    "github.review_pr",
    "github.request_changes",
    "github.merge_pr",
    "github.list_checks",
    "github.get_check_logs",
    "github.get_file_at_ref",
    "github.search_code",
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
    "test.run_tests",
    "test.run_subset",
    "test.coverage",
    "test.lint",
    "test.typecheck",
    "test.format",
    "ci.trigger_ci",
    "ci.get_ci_status",
}


# ---- tiny dummy tools, defined entirely here (the registry has never heard of
#      them — that is the whole point of the open/closed proof) ----------------
class _NoInput(Schema):
    """An empty, valid input contract for a dummy tool."""


class _NoOutput(Schema):
    """An empty, valid output contract for a dummy tool."""


def _make_tool(name: str, description: str) -> Tool:
    """Build and instantiate a one-off dummy Tool subclass for a test."""
    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {
            "name": name,
            "description": description,
            "Input": _NoInput,
            "Output": _NoOutput,
            "run": lambda self, request: _NoOutput(),
        },
    )
    return cls()


def _populated() -> ToolRegistry:
    """A registry with three distinct, unrelated tools registered (none loaded)."""
    registry = ToolRegistry()
    registry.register(_make_tool("ast.rename_symbol", "Rename a symbol across every reference safely."))
    registry.register(_make_tool("test.run_tests", "Run the test suite and report failures."))
    registry.register(_make_tool("git.status", "Show the working-tree status of the repository."))
    return registry


def _active_names(registry: ToolRegistry) -> list[str]:
    return [schema["function"]["name"] for schema in registry.active_schemas()]


# ---- the core is always present, the menu never is --------------------------
def test_core_tools_are_always_present_on_an_empty_registry() -> None:
    registry = ToolRegistry()
    names = _active_names(registry)
    # The fixed protocol surface: discover/load, the two plan verbs (property #3),
    # and the terminal verb. These act on the registry or the run, never the repo.
    assert set(CORE_TOOL_NAMES) == {
        SEARCH_TOOLS,
        LOAD_TOOL,
        PLAN_ADD,
        PLAN_RESOLVE,
        FINISH,
    }
    for core in CORE_TOOL_NAMES:
        assert core in names


def test_default_registry_registers_all_declared_tools_at_import() -> None:
    assert set(REGISTRY.tool_names()) == _DECLARED_TOOL_NAMES
    assert len(REGISTRY.tool_names()) == 57
    assert REGISTRY.loaded_names() == ()
    assert set(_active_names(REGISTRY)) == set(CORE_TOOL_NAMES)


def test_core_tools_remain_present_after_a_load() -> None:
    registry = _populated()
    registry.load({"name": "git.status"})
    names = _active_names(registry)
    for core in CORE_TOOL_NAMES:
        assert core in names


def test_unloaded_tools_never_appear_in_active_schemas() -> None:
    registry = _populated()
    registry.load({"name": "git.status"})

    names = _active_names(registry)
    assert "git_status" in names  # the one we loaded, OpenAI-safe name
    assert "ast_rename_symbol" not in names  # registered but never loaded
    assert "test_run_tests" not in names


def test_load_makes_a_tool_active() -> None:
    registry = _populated()
    assert "ast_rename_symbol" not in _active_names(registry)

    result = registry.load({"name": "ast.rename_symbol"})

    assert result.name == "ast.rename_symbol"
    assert result.loaded is True
    assert registry.is_loaded("ast.rename_symbol")
    assert "ast_rename_symbol" in _active_names(registry)


def test_active_schemas_is_exactly_core_plus_loaded() -> None:
    registry = _populated()
    registry.load({"name": "git.status"})
    registry.load({"name": "ast.rename_symbol"})

    assert set(_active_names(registry)) == set(CORE_TOOL_NAMES) | {
        "git_status",
        "ast_rename_symbol",
    }


# ---- search: ranked one-line summaries, NOT full schemas --------------------
def test_search_ranks_the_target_tool_first() -> None:
    registry = _populated()

    result = registry.search({"query": "rename symbol references"})

    assert result.summaries  # something ranked
    assert result.summaries[0].name == "ast.rename_symbol"


def test_search_keeps_the_target_within_top_k() -> None:
    registry = _populated()

    result = registry.search({"query": "run the failing tests", "limit": 2})

    names = [summary.name for summary in result.summaries]
    assert len(names) <= 2
    assert "test.run_tests" in names


def test_search_returns_summaries_not_full_schemas() -> None:
    registry = _populated()

    result = registry.search({"query": "status"})

    assert result.summaries
    summary = result.summaries[0]
    assert isinstance(summary, ToolSummary)
    # A summary carries only the three legible fields — no parameters/properties,
    # i.e. no full schema is leaked into the search result (ARCHITECTURE.md §5.2).
    assert set(summary.model_dump().keys()) == {"name", "namespace", "description"}
    dumped = result.model_dump_json()
    assert "parameters" not in dumped
    assert "properties" not in dumped


def test_summary_namespace_is_derived_from_the_name() -> None:
    registry = _populated()
    result = registry.search({"query": "rename symbol"})
    summary = result.summaries[0]
    assert summary.namespace == "ast"


def test_ranking_is_deterministic_and_tie_breaks_by_name() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("ast.beta_finder", "find references usage"))
    registry.register(_make_tool("ast.alpha_finder", "find references usage"))

    first = [s.name for s in registry.search({"query": "find"}).summaries]
    second = [s.name for s in registry.search({"query": "find"}).summaries]

    assert first == second  # deterministic
    assert first == ["ast.alpha_finder", "ast.beta_finder"]  # equal score → name order


# ---- open/closed: a new tool needs ZERO registry edits ----------------------
def test_a_brand_new_tool_is_fully_usable_without_touching_the_registry() -> None:
    """The anti-50-branch proof: a tool registry.py has never named works anyway."""
    registry = ToolRegistry()
    registry.register(_make_tool("fs.apply_patch", "Apply a unified diff patch to a file on disk."))

    found = [s.name for s in registry.search({"query": "apply patch diff"}).summaries]
    assert "fs.apply_patch" in found

    registry.load({"name": "fs.apply_patch"})
    assert "fs_apply_patch" in _active_names(registry)


def test_registry_source_hardcodes_no_concrete_tool_name() -> None:
    """Structural guard: dispatch is generic — no per-tool conditional anywhere."""
    source = _REGISTRY_SRC.read_text()
    for concrete in ("find_references", "rename_symbol", "run_tests", "apply_patch"):
        assert concrete not in source


# ---- edges: empty query, unknown name, malformed input → typed --------------
def test_empty_query_returns_no_summaries_rather_than_erroring() -> None:
    registry = _populated()
    result = registry.search({"query": ""})
    assert result.summaries == []


def test_loading_an_unknown_tool_raises_not_found() -> None:
    registry = _populated()
    with pytest.raises(NotFoundError):
        registry.load({"name": "ast.does_not_exist"})


def test_loading_is_idempotent() -> None:
    registry = _populated()
    registry.load({"name": "git.status"})
    registry.load({"name": "git.status"})
    assert registry.loaded_names() == ("git.status",)


def test_registering_a_duplicate_name_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("git.status", "Show status."))
    with pytest.raises(ValueError):
        registry.register(_make_tool("git.status", "A clashing second status tool."))


def test_malformed_search_input_is_a_typed_error() -> None:
    registry = _populated()
    with pytest.raises(MalformedInputError):
        registry.search({"query": "ok", "unexpected_field": True})


def test_malformed_load_input_is_a_typed_error() -> None:
    registry = _populated()
    with pytest.raises(MalformedInputError):
        registry.load({"wrong_key": "git.status"})


# ---- finish terminates with a typed structured result -----------------------
def test_finish_returns_the_structured_result() -> None:
    registry = ToolRegistry()
    result = registry.finish({"result": "renamed calculate_total in 7 files; tests green"})
    assert result.result == "renamed calculate_total in 7 files; tests green"


# ---- the plan protocol verbs (property #3) ----------------------------------
def test_plan_add_echoes_the_declared_sub_goals() -> None:
    registry = ToolRegistry()
    result = registry.dispatch(PLAN_ADD, {"sub_goals": ["map call sites", "edit them"]})
    assert result.sub_goals == ["map call sites", "edit them"]


def test_plan_resolve_echoes_the_completed_sub_goal() -> None:
    registry = ToolRegistry()
    result = registry.dispatch(PLAN_RESOLVE, {"sub_goal": "map call sites"})
    assert result.resolved == ["map call sites"]


def test_plan_verbs_reject_malformed_input() -> None:
    registry = ToolRegistry()
    with pytest.raises(MalformedInputError):
        registry.plan_add({"sub_goals": []})  # a plan must have at least one step
    with pytest.raises(MalformedInputError):
        registry.plan_resolve({"wrong_key": "x"})


def test_plan_verbs_are_dispatchable_without_being_loaded() -> None:
    """They are protocol, not repo-acting tools — always available, never loaded."""
    registry = ToolRegistry()
    assert registry.loaded_names() == ()
    assert registry.dispatch(PLAN_ADD, {"sub_goals": ["a"]}).sub_goals == ["a"]


# ---- the ARCH_DEBT_001 research-retry trace tag -----------------------------
def test_consecutive_searches_emit_the_research_retry_tag() -> None:
    emitted: list[tuple[str, str]] = []
    registry = ToolRegistry(tracer=lambda tag, message: emitted.append((tag, message)))

    registry.register(_make_tool("git.status", "Show status."))
    registry.search({"query": "status"})
    assert all(tag != RESEARCH_RETRY_TAG for tag, _ in emitted)  # first search is clean

    registry.search({"query": "status again"})  # re-search with nothing loaded between
    assert any(tag == RESEARCH_RETRY_TAG for tag, _ in emitted)


def test_a_load_between_searches_resets_the_retry_signal() -> None:
    emitted: list[tuple[str, str]] = []
    registry = ToolRegistry(tracer=lambda tag, message: emitted.append((tag, message)))
    registry.register(_make_tool("git.status", "Show status."))

    registry.search({"query": "status"})
    registry.load({"name": "git.status"})
    registry.search({"query": "status again"})

    assert all(tag != RESEARCH_RETRY_TAG for tag, _ in emitted)


# ---- the registry accepts both typed models and raw dicts -------------------
def test_search_and_load_accept_typed_requests_too() -> None:
    from limpiador.schemas import LoadToolRequest, SearchToolsRequest

    registry = _populated()
    result = registry.search(SearchToolsRequest(query="rename symbol"))
    assert result.summaries[0].name == "ast.rename_symbol"

    loaded = registry.load(LoadToolRequest(name="ast.rename_symbol"))
    assert loaded.loaded is True


# ---- declared-but-unimplemented shells: registered, schema-valid, yet typed-
#      failing if actually invoked (the contract that makes import-time
#      registration of all 56 safe before the executors land) -----------------
def test_a_declared_tool_loads_with_a_valid_openai_schema() -> None:
    from limpiador.tools import git_tools

    registry = ToolRegistry()  # isolated — never mutate the shared default REGISTRY
    for tool in git_tools.TOOLS:
        registry.register(tool)
    registry.load({"name": "git.status"})

    schema = next(
        s for s in registry.active_schemas() if s["function"]["name"] == "git_status"
    )
    assert schema["type"] == "function"
    assert schema["function"]["parameters"]["additionalProperties"] is False


def test_invoking_a_declared_but_unimplemented_tool_raises_a_typed_error() -> None:
    # Every shipped namespace is now fully implemented, so this exercises the
    # declared_tool *mechanism* directly: a capability shell with typed I/O and
    # no executor yet is the contract a not-yet-built tool is registered under.
    from limpiador.observability.errors import ToolError, ToolUnavailableError
    from limpiador.tools.base import declared_tool

    declared = declared_tool("ci.trigger_ci", "A not-yet-built capability.", _NoInput, _NoOutput)
    with pytest.raises(ToolUnavailableError) as caught:
        declared.invoke(_NoInput())
    # It is a recoverable ToolError, so the loop can fold it back into context
    # rather than crashing on an unimplemented capability.
    assert isinstance(caught.value, ToolError)


# ---- the StaticRegistry baseline: the rejected alternative, made measurable ---
# ``ToolRegistry`` hides the full menu behind discovery; ``StaticRegistry`` is the
# design it argues against — hand the model everything, every turn — kept as a
# first-class baseline so the ablation (evals/ablation.py) can put a number on the
# difference. These tests pin the one property that makes it that baseline.
def test_static_registry_hands_over_the_whole_menu_every_turn() -> None:
    from limpiador.tools.registry import build_static_registry

    registry = build_static_registry()
    active = set(_active_names(registry))

    # Every repo tool is present up front, by its OpenAI-safe name — the opposite
    # of the dynamic registry, where none appear until loaded.
    for name in _DECLARED_TOOL_NAMES:
        assert name.replace(".", "_") in active
    # The loop's protocol verbs stay (it depends on them)...
    assert {PLAN_ADD, PLAN_RESOLVE, FINISH} <= active
    # ...but the two *discovery* verbs are gone: there is nothing to discover when
    # the whole catalogue is already on the table.
    assert SEARCH_TOOLS not in active
    assert LOAD_TOOL not in active
    # 57 repo tools + the three protocol verbs, and nothing else.
    assert len(active) == len(_DECLARED_TOOL_NAMES) + 3


def test_static_registry_pre_loads_every_tool_so_a_call_needs_no_discovery() -> None:
    from limpiador.tools.registry import build_static_registry

    registry = build_static_registry()
    # Every declared tool is already loaded, so dispatch resolves a call the model
    # makes without a preceding load_tool — which is what "hand it everything" means.
    assert all(registry.is_loaded(name) for name in registry.tool_names())

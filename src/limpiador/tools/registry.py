"""Dynamic tool loading and search (ARCHITECTURE.md §5, property #1).

The registry holds all fifty-seven tools registered at import time, tracks which
are currently loaded into context, and exposes only ``core + loaded`` schemas to
the LLM adapter each turn. The model always sees a small fixed core —
``search_tools(query)``, ``load_tool(name)``, the plan verbs ``plan_add`` /
``plan_resolve``, and ``finish(result)`` — and discovers everything else, which is
what *proves* model-driven selection: the model cannot fall back on a tool it was
handed because it was handed almost nothing. None of the core acts on the repo:
they act on the registry, or on the run's own plan.

Search ranking is a local, deterministic operation over tool names and
descriptions — no model call, no cost. The current keyword-overlap strategy and
its known limitation are tracked as ARCH_DEBT_001 in .clauderules; a re-search
with nothing loaded in between emits the ``[REGISTRY RESEARCH_RETRY]`` trace tag
so that limitation's frequency can be measured.

Nothing in this module branches on the identity of any concrete tool. Dispatch
is generic — register a tool, and ``search``/``load``/``active_schemas`` handle
it through the same code path as every other. That open/closed property is the
whole point: it is how fifty tools stay coherent without collapsing into a chain
of fifty conditional dispatches.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import ValidationError

from limpiador.observability.errors import MalformedInputError, NotFoundError
from limpiador.observability.tracing import RESEARCH_RETRY_TAG
from limpiador.observability.tracing import emit as _default_emit
from limpiador.schemas import (
    FinishRequest,
    FinishResult,
    LoadToolRequest,
    LoadToolResult,
    PlanAddRequest,
    PlanResolveRequest,
    PlanResult,
    Schema,
    SearchToolsRequest,
    SearchToolsResult,
    ToolSummary,
)
from limpiador.tools.base import Tool, openai_function_schema

# The fixed core the model always sees (ARCHITECTURE.md §5.2). These names are
# not in any namespace — they operate on the registry or the run itself, not the
# repo — so they live here rather than as namespaced Tool subclasses.
SEARCH_TOOLS = "search_tools"
LOAD_TOOL = "load_tool"
FINISH = "finish"
# The plan protocol verbs (property #3). Like ``finish``, these do not act on the
# repo — they act on the *run*, recording the agent's plan in durable working
# memory so it survives compaction. The registry validates and echoes them; the
# loop applies their meaning to the Context, exactly as it does for ``finish``.
PLAN_ADD = "plan_add"
PLAN_RESOLVE = "plan_resolve"
CORE_TOOL_NAMES: tuple[str, ...] = (SEARCH_TOOLS, LOAD_TOOL, PLAN_ADD, PLAN_RESOLVE, FINISH)

# Ranking weights: a hit in the tool's *name* is a stronger capability signal
# than a hit in its prose description, so name overlap is weighted more heavily.
_NAME_WEIGHT = 2
_DESCRIPTION_WEIGHT = 1

Tracer = Callable[[str, str], None]
_RequestT = TypeVar("_RequestT", bound=Schema)


@dataclass(frozen=True)
class _CoreTool:
    """A core meta-tool's wire identity: its name, description, and input model."""

    name: str
    description: str
    input_model: type[Schema]


# The core meta-tools' schemas. Descriptions are written for the model to read —
# they tell it how the discover→load→finish loop works.
_CORE_TOOLS: tuple[_CoreTool, ...] = (
    _CoreTool(
        SEARCH_TOOLS,
        "Search the tool registry by capability. Returns ranked one-line tool "
        "summaries (name + description), NOT full schemas. Call this first to "
        "discover which tool you need.",
        SearchToolsRequest,
    ),
    _CoreTool(
        LOAD_TOOL,
        "Load a discovered tool by its '<namespace>.<tool>' name so its full "
        "schema becomes available to call on the next turn.",
        LoadToolRequest,
    ),
    _CoreTool(
        PLAN_ADD,
        "Commit to a plan: record the sub-goals you intend to work through, in "
        "order. Call this once, early, before you start investigating. The plan "
        "is durable — it survives context compaction, so you will not lose it on "
        "a long task.",
        PlanAddRequest,
    ),
    _CoreTool(
        PLAN_RESOLVE,
        "Mark one planned sub-goal complete. Resolved sub-goals are durable and "
        "must never be re-opened or redone — consult the plan instead of redoing "
        "finished work.",
        PlanResolveRequest,
    ),
    _CoreTool(
        FINISH,
        "Finish the task and return the final structured result. Call this once "
        "the objective is complete.",
        FinishRequest,
    ),
)


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens — the unit both ranking sides are split into.

    Splitting on every non-alphanumeric boundary means a namespaced name like
    ``ns.do_thing`` yields ``{ns, do, thing}``, so a query whose words match the
    name parts overlaps it directly.
    """
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class ToolRegistry:
    """Holds every tool, tracks which are loaded, and serves the active schemas.

    The model interacts with the registry only through the core meta-tools
    (:meth:`search`, :meth:`load`, :meth:`finish`); :meth:`active_schemas` is what
    the loop hands the adapter each turn. Construction takes an optional ``tracer``
    so a test can assert on debt-tag emissions without touching global logging.
    """

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._loaded: set[str] = set()
        self._trace: Tracer = tracer if tracer is not None else _default_emit
        # Tracks whether the immediately preceding registry action was a search,
        # so a search-then-search with no load between can be flagged as an
        # ARCH_DEBT_001 research-retry (a wasted turn).
        self._last_was_search = False

    # ---- registration -------------------------------------------------------
    def register(self, tool: Tool) -> None:
        """Add a tool to the registry. A duplicate name is a developer error."""
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def tool_names(self) -> tuple[str, ...]:
        """Every registered tool's canonical name, sorted."""
        return tuple(sorted(self._tools))

    def loaded_names(self) -> tuple[str, ...]:
        """The canonical names of the currently-loaded tools, sorted."""
        return tuple(sorted(self._loaded))

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    # ---- the core meta-tools ------------------------------------------------
    def search(self, request: SearchToolsRequest | dict[str, object]) -> SearchToolsResult:
        """Rank tools against a capability query; return one-line summaries (§5.2).

        A re-search with nothing loaded since the last one means the previous
        result did not let the model commit to a tool — the ARCH_DEBT_001 failure
        mode — so it emits the research-retry trace tag before ranking again.
        """
        req = self._coerce(SearchToolsRequest, request)
        if self._last_was_search:
            self._trace(RESEARCH_RETRY_TAG, f"query={req.query!r}")
        self._last_was_search = True

        ranked = self._rank(req.query)
        summaries = [self._summarize(tool) for tool in ranked[: req.limit]]
        return SearchToolsResult(summaries=summaries)

    def load(self, request: LoadToolRequest | dict[str, object]) -> LoadToolResult:
        """Promote a discovered tool into the active set. Unknown name → NotFound."""
        req = self._coerce(LoadToolRequest, request)
        if req.name not in self._tools:
            raise NotFoundError(
                f"No tool named {req.name!r}; use search_tools to discover available tools."
            )
        self._loaded.add(req.name)
        self._last_was_search = False
        return LoadToolResult(name=req.name, loaded=True)

    def plan_add(self, request: PlanAddRequest | dict[str, object]) -> PlanResult:
        """Acknowledge a declared plan; the loop commits it to durable memory."""
        req = self._coerce(PlanAddRequest, request)
        self._last_was_search = False
        return PlanResult(sub_goals=list(req.sub_goals))

    def plan_resolve(self, request: PlanResolveRequest | dict[str, object]) -> PlanResult:
        """Acknowledge a completed sub-goal; the loop marks it resolved durably."""
        req = self._coerce(PlanResolveRequest, request)
        self._last_was_search = False
        return PlanResult(resolved=[req.sub_goal])

    def finish(self, request: FinishRequest | dict[str, object]) -> FinishResult:
        """Terminate the task with a structured result (the loop stops on this)."""
        req = self._coerce(FinishRequest, request)
        self._last_was_search = False
        return FinishResult(result=req.result)

    # ---- dispatch: route one tool call to its handler (the loop's seam) -----
    def dispatch(self, name: str, arguments: dict[str, object] | Schema) -> Schema:
        """Route a tool call (by its OpenAI-safe name) to a handler and invoke it.

        The core meta-tools are dispatched by their fixed protocol names;
        every other call routes to a *loaded* namespaced tool by its OpenAI-safe
        name. This is the registry's single seam for the loop: the loop hands over
        a name and arguments and never has to know which concrete tool runs. A
        name matching no core verb and no loaded tool is a recoverable
        :class:`NotFoundError` (the model called something it never loaded), folded
        back into context by the loop rather than crashing the run.
        """
        if name == SEARCH_TOOLS:
            return self.search(arguments)
        if name == LOAD_TOOL:
            return self.load(arguments)
        if name == PLAN_ADD:
            return self.plan_add(arguments)
        if name == PLAN_RESOLVE:
            return self.plan_resolve(arguments)
        if name == FINISH:
            return self.finish(arguments)
        return self._loaded_tool(name).invoke(arguments)

    def _loaded_tool(self, openai_name: str) -> Tool:
        """Resolve a loaded namespaced tool by its OpenAI-safe name, or raise."""
        for name in self._loaded:
            tool = self._tools[name]
            if tool.openai_name() == openai_name:
                return tool
        raise NotFoundError(
            f"Tool {openai_name!r} is not loaded; load it with load_tool before calling it."
        )

    # ---- what the loop hands the adapter each turn --------------------------
    def active_schemas(self) -> list[dict[str, object]]:
        """The OpenAI schemas the model sees this turn: core + loaded, nothing more.

        The full menu is never here — that is the property that forces the model
        to discover and load, rather than fall back on a handed tool (§5.2).
        """
        schemas = [
            openai_function_schema(core.name, core.description, core.input_model)
            for core in _CORE_TOOLS
        ]
        schemas.extend(self._tools[name].openai_schema() for name in sorted(self._loaded))
        return schemas

    # ---- ranking (local, deterministic — no model call) --------------------
    def _rank(self, query: str) -> list[Tool]:
        """Order tools by keyword overlap with the query (ARCH_DEBT_001).

        Empty query → empty result: there is nothing to rank on. Ties break by
        canonical name so the order is fully deterministic.
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        scored: list[tuple[int, str, Tool]] = []
        for tool in self._tools.values():
            score = self._score(tool, query_tokens)
            if score > 0:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored]

    @staticmethod
    def _score(tool: Tool, query_tokens: set[str]) -> int:
        """Weighted keyword overlap: name hits count for more than description hits."""
        name_hits = len(query_tokens & _tokenize(tool.name))
        description_hits = len(query_tokens & _tokenize(tool.description))
        return _NAME_WEIGHT * name_hits + _DESCRIPTION_WEIGHT * description_hits

    @staticmethod
    def _summarize(tool: Tool) -> ToolSummary:
        """A one-line summary — name, namespace, description — never a full schema."""
        return ToolSummary(
            name=tool.name,
            namespace=tool.namespace(),
            description=tool.description,
        )

    @staticmethod
    def _coerce(model: type[_RequestT], request: _RequestT | dict[str, object]) -> _RequestT:
        """Validate a request into its typed model, raising a typed error if invalid."""
        if isinstance(request, model):
            return request
        if isinstance(request, dict):
            try:
                return model.model_validate(request)
            except ValidationError as error:
                raise MalformedInputError(
                    f"arguments do not satisfy {model.__name__}: {error}"
                ) from error
        raise MalformedInputError(
            f"expected {model.__name__} or a dict, got {type(request).__name__}."
        )


def _install_declared_tools(registry: ToolRegistry) -> None:
    """Register every namespace module's declared tools into ``registry``.

    Each namespace module exposes a ``TOOLS`` catalog of declared shells (typed
    I/O, no executor yet); the registry is the aggregator that pulls them in.
    Dependencies point one way — registry → namespace modules → base/schemas —
    so there is no import cycle, and adding a tool means editing only its
    namespace catalog, never this loop (the open/closed property of §5).
    """
    from limpiador.tools import (
        ast_tools,
        fs_tools,
        git_tools,
        github_tools,
        test_tools,
    )

    for module in (git_tools, github_tools, fs_tools, ast_tools, test_tools):
        for tool in module.TOOLS:
            registry.register(tool)


def build_registry() -> ToolRegistry:
    """A fresh registry populated with every declared tool — the agent's full menu.

    The application :data:`REGISTRY` is one of these. A caller that needs an
    *isolated* full registry (a reproduction run that should not inherit another
    run's loaded-tool state, say) builds its own with this rather than sharing the
    module singleton.
    """
    registry = ToolRegistry()
    _install_declared_tools(registry)
    return registry


# The protocol verbs act on the *run*, not the repo — plan bookkeeping and the
# terminal signal — so the loop's control flow depends on them regardless of how
# tools are presented. They are the core minus the two *discovery* verbs
# (``search_tools`` / ``load_tool``). A static, full-menu registry keeps these and
# drops discovery; the dynamic one keeps all five.
_PROTOCOL_CORE_NAMES: tuple[str, ...] = (PLAN_ADD, PLAN_RESOLVE, FINISH)


class StaticRegistry(ToolRegistry):
    """The rejected alternative, kept measurable (ARCHITECTURE.md §15, MEMO).

    limpiador argues *against* the "register everything, hand the model all fifty
    schemas on every turn, let it pick" design, in favour of dynamic discovery
    (:class:`ToolRegistry`). This subclass *is* that alternative, preserved as a
    first-class, greppable baseline so the argument can be *measured* rather than
    merely asserted: the ablation harness (``python -m evals.ablation``) drives the
    same cases through both and compares tokens, turns, and selection.

    It presents the loop's protocol verbs plus *every* repo tool as directly
    callable, and omits ``search_tools`` / ``load_tool`` entirely — discovery is
    precisely the mechanism this baseline removes. Every declared tool is
    pre-loaded (see :func:`build_static_registry`), so a call the model makes
    dispatches through the inherited path without a preceding ``load_tool``.
    """

    def active_schemas(self) -> list[dict[str, object]]:
        """The full menu: the protocol verbs plus every repo tool, on every turn.

        This is the whole point of the baseline — the property :class:`ToolRegistry`
        is built to *avoid*. The footprint scales with the size of the catalogue,
        replayed on each of a run's twenty-plus turns, not with what a task uses.
        """
        schemas = [
            openai_function_schema(core.name, core.description, core.input_model)
            for core in _CORE_TOOLS
            if core.name in _PROTOCOL_CORE_NAMES
        ]
        schemas.extend(self._tools[name].openai_schema() for name in sorted(self._loaded))
        return schemas


def build_static_registry() -> StaticRegistry:
    """A :class:`StaticRegistry` with every declared tool registered *and loaded*.

    Pre-loading through the public :meth:`~ToolRegistry.load` seam means dispatch
    resolves any repo tool the model calls, exactly as if it had discovered and
    loaded it first — the baseline just hands the whole catalogue over up front.
    """
    registry = StaticRegistry()
    _install_declared_tools(registry)
    for name in registry.tool_names():
        registry.load(LoadToolRequest(name=name))
    return registry


# The default application registry: the core meta-tools plus all fifty-seven
# declared tools, registered at import. Tests build their own isolated
# ToolRegistry instances; this is the one the agent loop runs against.
REGISTRY = build_registry()

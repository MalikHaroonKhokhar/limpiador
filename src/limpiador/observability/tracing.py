"""Structured tracing and token accounting (ARCHITECTURE.md §13).

Every tool call and every model call is recorded structurally — which tool, with
what input, returning what, how long it took, how many tokens. The trace is what
the eval harness asserts against (does the agent reason in the right order, under
the call ceiling) and what the demo surfaces. Token accounting lives here;
per-dollar budgeting is deliberately out of scope (§14), but knowing the token
cost of a run is basic observability. Debt-tracker trace tags (e.g.
``[REGISTRY RESEARCH_RETRY]``, ``[CONTEXT REREAD]``) are emitted here so their
frequency can be measured (.clauderules §8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from limpiador.schemas import TokenUsage

logger = logging.getLogger("limpiador.trace")

# Debt-tracker trace tags. Counting these in a run's trace is how we measure
# whether a known limitation fires often enough to be worth fixing (.clauderules
# §8). ``[REGISTRY RESEARCH_RETRY]`` is ARCH_DEBT_001: the keyword ranker sent
# the model back to ``search_tools`` instead of letting it load on the first try.
RESEARCH_RETRY_TAG = "[REGISTRY RESEARCH_RETRY]"

# ``[CONTEXT REREAD]`` is ARCH_DEBT_002: a file was read twice in one run — the
# signal that summarize-then-evict dropped a raw payload the agent later needed.
# Keeping symbol facts durable is the mitigation; counting this tag is how we
# learn whether re-reads stay rare enough to leave that mitigation as-is.
CONTEXT_REREAD_TAG = "[CONTEXT REREAD]"

# ``[ROUTING]`` is an observability tag, not a debt one: every model call records
# which turn kind it was, which model served it, and the stable-prefix fingerprint
# that prompt caching keys on. Counting these is how a run shows the bulk of calls
# went to the cheap model and that the cached head stayed stable across turns.
ROUTING_TAG = "[ROUTING]"


def emit(tag: str, message: str = "") -> None:
    """Record a tagged trace event so its frequency can be counted later.

    Intentionally thin: it logs through the ``limpiador.trace`` logger so a tag
    surfaces in a run trace without any subsystem depending on a richer tracer.
    Components that need to assert on emissions in a test inject their own
    callable instead of reaching for this default — most often a :class:`Tracer`,
    which is itself a ``(tag, message)`` callable.
    """
    if message:
        logger.info("%s %s", tag, message)
    else:
        logger.info("%s", tag)


# ============================================================================
# Structured trace — the substrate the eval harness asserts against (§13)
# ============================================================================


class CallKind(str, Enum):
    """The two kinds of call a run makes: a model turn and a tool dispatch."""

    MODEL = "model"
    TOOL = "tool"


@dataclass(frozen=True)
class TraceEntry:
    """One recorded call — a structured object, never a log string.

    Model calls carry ``usage``, the ``route`` decision (which turn kind), and a
    priced ``cost_usd`` when the adapter could price the model; tool calls carry
    their ``input``/``output`` and an ``error`` kind when the call failed and was
    folded back as recoverable. ``latency_s`` is wall-clock for either.
    """

    kind: CallKind
    name: str
    latency_s: float
    input: Any = None
    output: Any = None
    usage: TokenUsage | None = None
    route: str | None = None
    error: str | None = None
    cost_usd: float | None = None


class Tracer:
    """Collects structured trace entries and tagged events for one run.

    It is also a ``(tag, message)`` callable, so it drops straight into the
    tracer seam the adapter, registry, and context already accept — passing one
    Tracer to all of them funnels every structured call *and* every debt/
    observability tag into a single run trace. The eval-harness helpers
    (:meth:`called`, :meth:`order`, :meth:`call_count`) and the token/cost totals
    read off that trace.
    """

    def __init__(self) -> None:
        self._entries: list[TraceEntry] = []
        self._tags: list[tuple[str, str]] = []

    # ---- recording ----------------------------------------------------------
    def record_model_call(
        self,
        *,
        model: str | None,
        latency_s: float,
        input: Any = None,
        output: Any = None,
        usage: TokenUsage | None = None,
        route: str | None = None,
        cost_usd: float | None = None,
    ) -> TraceEntry:
        """Record one model turn. ``model`` falls back to a neutral label if absent."""
        entry = TraceEntry(
            CallKind.MODEL,
            model or "model",
            latency_s,
            input=input,
            output=output,
            usage=usage,
            route=route,
            cost_usd=cost_usd,
        )
        self._entries.append(entry)
        return entry

    def record_tool_call(
        self,
        *,
        tool: str,
        latency_s: float,
        input: Any = None,
        output: Any = None,
        error: str | None = None,
    ) -> TraceEntry:
        """Record one tool dispatch, including the error kind if it failed."""
        entry = TraceEntry(
            CallKind.TOOL, tool, latency_s, input=input, output=output, error=error
        )
        self._entries.append(entry)
        return entry

    def tag(self, tag: str, message: str = "") -> None:
        """Capture a tagged event (the debt/observability tags)."""
        self._tags.append((tag, message))

    # The ``(tag, message)`` tracer seam: ``tracer(TAG, msg)`` records a tag, so a
    # Tracer is interchangeable with :func:`emit` everywhere a tracer is injected.
    __call__ = tag

    # ---- views --------------------------------------------------------------
    @property
    def entries(self) -> tuple[TraceEntry, ...]:
        return tuple(self._entries)

    @property
    def tags(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._tags)

    @property
    def model_calls(self) -> tuple[TraceEntry, ...]:
        return tuple(e for e in self._entries if e.kind is CallKind.MODEL)

    @property
    def tool_calls(self) -> tuple[TraceEntry, ...]:
        return tuple(e for e in self._entries if e.kind is CallKind.TOOL)

    # ---- eval-harness helpers (over tool calls) -----------------------------
    def called(self, tool: str) -> bool:
        """Did ``tool`` run at least once in this run?"""
        return any(e.name == tool for e in self.tool_calls)

    def order(self, a: str, b: str) -> bool:
        """Did ``a`` first run strictly before ``b``? False if either never ran."""
        names = [e.name for e in self.tool_calls]
        if a not in names or b not in names:
            return False
        return names.index(a) < names.index(b)

    def call_count(self, name: str | None = None) -> int:
        """Total tool calls, or the count for one tool name."""
        if name is None:
            return len(self.tool_calls)
        return sum(1 for e in self.tool_calls if e.name == name)

    # ---- token / cost accounting (over model calls) -------------------------
    def total_prompt_tokens(self) -> int:
        return sum(e.usage.prompt_tokens for e in self.model_calls if e.usage is not None)

    def total_completion_tokens(self) -> int:
        return sum(e.usage.completion_tokens for e in self.model_calls if e.usage is not None)

    def total_tokens(self) -> int:
        return self.total_prompt_tokens() + self.total_completion_tokens()

    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.model_calls if e.cost_usd is not None)

    # ---- tag accounting -----------------------------------------------------
    def has_tag(self, tag: str) -> bool:
        return any(t == tag for t, _ in self._tags)

    def count_tag(self, tag: str) -> int:
        return sum(1 for t, _ in self._tags if t == tag)

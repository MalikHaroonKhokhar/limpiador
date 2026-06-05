"""The orchestration spine (ARCHITECTURE.md §6).

One turn of the loop: guard-check the call ceiling, assemble context (message
history plus the ``core + loaded`` tool schemas from the registry), call the
model, dispatch each tool call it asks for, fold the typed results back in,
compact if the footprint crosses the threshold, then terminate on ``finish`` or
repeat.

The loop does only orchestration. It does not know what any individual tool
does, does not parse free text, and does not branch on tool identity — that
ignorance is deliberate, and it is what keeps the system out of the
fifty-conditional-dispatch anti-pattern the brief warns against. The single
exception is the ``finish`` protocol verb: recognizing the one terminal signal
is how a turn cycle ends, the way a function recognizes ``return``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from limpiador.agent.context import (
    DEFAULT_COMPACTION_THRESHOLD_TOKENS,
    Context,
    PayloadKind,
)
from limpiador.agent.guard import CallGuard, RunAborted
from limpiador.agent.llm import LLMAdapter, Messages
from limpiador.observability.errors import ToolError
from limpiador.observability.tracing import Tracer
from limpiador.schemas import LLMResponse, ToolCall
from limpiador.tools.registry import FINISH, ToolRegistry


@dataclass(frozen=True)
class RunResult:
    """The outcome of a run: what the agent concluded, and a thin trace of how.

    ``result`` is the ``finish`` payload (or trailing model text) when the run
    completed; it is ``None`` when ``aborted`` is true. ``tool_calls`` is the
    ordered list of OpenAI-safe names dispatched, and ``messages`` is the full
    transcript — enough for a test to assert the loop folded a failure back in
    rather than crashing on it.
    """

    result: str | None
    aborted: bool
    turns: int
    tool_calls: tuple[str, ...]
    messages: list[dict[str, object]] = field(default_factory=list)
    trace: Tracer | None = None


def run(
    task: str,
    *,
    registry: ToolRegistry,
    adapter: LLMAdapter,
    guard: CallGuard | None = None,
    system_prompt: str | None = None,
    threshold_tokens: int = DEFAULT_COMPACTION_THRESHOLD_TOKENS,
    tracer: Tracer | None = None,
) -> RunResult:
    """Drive the agent loop to completion (``finish``) or a guarded abort.

    Each turn: guard-check the ceiling, assemble the active schemas, call the
    model, dispatch every tool call it returns, fold each typed result — or a
    structured error for a failed call — back into the transcript, then terminate
    on ``finish`` or repeat.

    "OpenAI may return several tool calls at once" is the provider's *parallel
    tool calls* — several calls in one assistant turn, not a request for
    concurrency. They are dispatched sequentially, in the order returned, because
    these tools have side effects (writes, git operations) where ordering is
    correctness, not latency: a deterministic in-order batch is the safe choice.
    """
    guard = guard or CallGuard()
    tracer = tracer or Tracer()
    messages: Messages = _initial_messages(task, system_prompt)
    # Working memory (property #3, §7): the loop folds every raw tool result into
    # the context as an opaque payload and compacts when the footprint crosses the
    # threshold — never inspecting what any result *is*, so it stays tool-ignorant.
    # The same tracer captures the context's tags, so the run trace is unified.
    context = Context(task, threshold_tokens=threshold_tokens, tracer=tracer)
    dispatched: list[str] = []
    turns = 0

    while True:
        try:
            guard.check()
        except RunAborted:
            return RunResult(None, aborted=True, turns=turns, tool_calls=tuple(dispatched), messages=messages, trace=tracer)

        response = _timed_complete(adapter, messages, registry, tracer)
        turns += 1
        messages.append(_assistant_message(response))

        # A model turn with no tool calls is a plain answer — the run is done.
        if not response.tool_calls:
            return RunResult(response.text, aborted=False, turns=turns, tool_calls=tuple(dispatched), messages=messages, trace=tracer)

        # Dispatch the turn's calls sequentially, in order (see the docstring on
        # parallel-vs-concurrent), folding each typed result into the transcript.
        finished: str | None = None
        for call in response.tool_calls:
            guard.record()
            dispatched.append(call.name)
            message = _timed_dispatch(registry, call, tracer)
            messages.append(message)
            _record_payload(context, call, message)
            if call.name == FINISH and finished is None:
                finished = _finish_text(message)

        # Step 5 of §6 is "fold and compact". Folding happens above; this is the
        # compact half — property #3 (§7), now live. When the working set's
        # footprint crosses the threshold, the context summarizes-then-evicts the
        # stale raw payloads, and we sync those summaries back into the wire
        # transcript so the next model call carries the gist, not the bulk. A run
        # below threshold compacts nothing, so short runs are untouched.
        _compact(context, messages)

        if finished is not None:
            return RunResult(finished, aborted=False, turns=turns, tool_calls=tuple(dispatched), messages=messages, trace=tracer)


def _initial_messages(task: str, system_prompt: str | None) -> Messages:
    messages: Messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": task})
    return messages


def _assistant_message(response: LLMResponse) -> dict[str, object]:
    """Reconstruct the OpenAI-shaped assistant turn from the normalized response.

    Includes the requested tool calls so the following ``tool`` results reference
    them by id — the wire protocol a real provider expects on the next call.
    """
    message: dict[str, object] = {"role": "assistant", "content": response.text}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in response.tool_calls
        ]
    return message


def _dispatch_one(registry: ToolRegistry, call: ToolCall) -> dict[str, object]:
    """Run one tool call and return its folded ``tool`` message.

    A typed :class:`ToolError` is *recoverable*: it is folded back as a structured
    error result the model can read and adapt to, never re-raised. Only the
    failure kind and message cross the boundary — not a stack trace.
    """
    try:
        result = registry.dispatch(call.name, call.arguments)
        content = result.model_dump_json()
    except ToolError as error:
        content = json.dumps({"error": type(error).__name__, "message": str(error)})
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
    }


def _timed_complete(
    adapter: LLMAdapter, messages: Messages, registry: ToolRegistry, tracer: Tracer
) -> LLMResponse:
    """Call the model, timing it and recording a structured model-call entry.

    The model name, routing decision, token usage, and per-call cost ride in on
    the normalized response (the adapter annotated them); the loop adds the
    wall-clock latency it measured. For the mock those annotations are absent, so
    the entry records a model turn with no real model, route, or price.
    """
    start = time.perf_counter()
    response = adapter.complete(messages, tools=registry.active_schemas())
    latency = time.perf_counter() - start
    tracer.record_model_call(
        model=response.model,
        latency_s=latency,
        output=response.text,
        usage=response.usage,
        route=response.route,
        cost_usd=response.cost_usd,
    )
    return response


def _timed_dispatch(registry: ToolRegistry, call: ToolCall, tracer: Tracer) -> dict[str, object]:
    """Dispatch one tool call, timing it and recording a structured tool-call entry.

    The folded message is returned unchanged for the transcript; the trace entry
    captures the call's input, its output, and — when the call failed and was
    folded as recoverable — the error kind, so the trace tells the whole story.
    """
    start = time.perf_counter()
    message = _dispatch_one(registry, call)
    latency = time.perf_counter() - start
    content = str(message["content"])
    tracer.record_tool_call(
        tool=call.name,
        latency_s=latency,
        input=call.arguments,
        output=content,
        error=_error_kind(content),
    )
    return message


def _error_kind(content: str) -> str | None:
    """The error type from a folded error result, or None for a normal result."""
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and "error" in parsed:
        return str(parsed["error"])
    return None


def _record_payload(context: Context, call: ToolCall, message: dict[str, object]) -> None:
    """Fold a tool result into working memory as an opaque, evictable payload.

    Keyed by the call id so its summary can later be synced back onto the exact
    wire message. The kind is ``RESULT`` — the loop does not, and must not, know
    whether the result is a file, a diff, or a log; that classification belongs to
    a tool-aware layer, not the tool-ignorant spine.
    """
    content = str(message["content"])
    context.record_payload(
        call.id,
        content,
        kind=PayloadKind.RESULT,
        summary=f"[{call.name}] result elided after compaction ({len(content)} chars).",
    )


def _compact(context: Context, messages: Messages) -> None:
    """Compact working memory and mirror any eviction onto the wire transcript.

    The context decides *what* to evict (stale, unpinned, not the most recent);
    here we keep the OpenAI transcript valid by replacing only the evicted tool
    messages' content with their summary — the assistant/tool pairing and ids are
    untouched, so the next call is well-formed but lighter.
    """
    result = context.compact()
    if not result.evicted:
        return
    by_id = {m.get("tool_call_id"): m for m in messages if m.get("role") == "tool"}
    for payload in context.payloads:
        if payload.evicted and payload.key in by_id:
            by_id[payload.key]["content"] = payload.summary


def _finish_text(tool_message: dict[str, object]) -> str:
    """Extract the ``finish`` result text from its folded tool message."""
    return json.loads(str(tool_message["content"]))["result"]

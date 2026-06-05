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
from dataclasses import dataclass, field

from limpiador.agent.guard import CallGuard, RunAborted
from limpiador.agent.llm import LLMAdapter, Messages
from limpiador.observability.errors import ToolError
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


def run(
    task: str,
    *,
    registry: ToolRegistry,
    adapter: LLMAdapter,
    guard: CallGuard | None = None,
    system_prompt: str | None = None,
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
    messages: Messages = _initial_messages(task, system_prompt)
    dispatched: list[str] = []
    turns = 0

    while True:
        try:
            guard.check()
        except RunAborted:
            return RunResult(None, aborted=True, turns=turns, tool_calls=tuple(dispatched), messages=messages)

        response = adapter.complete(messages, tools=registry.active_schemas())
        turns += 1
        messages.append(_assistant_message(response))

        # A model turn with no tool calls is a plain answer — the run is done.
        if not response.tool_calls:
            return RunResult(response.text, aborted=False, turns=turns, tool_calls=tuple(dispatched), messages=messages)

        # Dispatch the turn's calls sequentially, in order (see the docstring on
        # parallel-vs-concurrent), folding each typed result into the transcript.
        finished: str | None = None
        for call in response.tool_calls:
            guard.record()
            dispatched.append(call.name)
            messages.append(_dispatch_one(registry, call))
            if call.name == FINISH and finished is None:
                finished = _finish_text(messages[-1])

        # Step 5 of §6 is "fold and compact". Folding happens above; compaction is
        # deliberately out of scope here — it is property #3 (§7), built in its
        # own ticket against the Context object (context.py). This is its seam:
        # when the transcript's token footprint crosses the threshold, the
        # eviction strategy runs here. Until then the loop is "fold now".

        if finished is not None:
            return RunResult(finished, aborted=False, turns=turns, tool_calls=tuple(dispatched), messages=messages)


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


def _finish_text(tool_message: dict[str, object]) -> str:
    """Extract the ``finish`` result text from its folded tool message."""
    return json.loads(str(tool_message["content"]))["result"]

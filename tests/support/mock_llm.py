"""Deterministic mock LLM adapter (ARCHITECTURE.md §10-11, .clauderules §5).

Implements the same ``LLMAdapter`` interface the real OpenAI adapter does and is
injected through it, so production code stays unaware a mock exists. It replays
a *scripted* sequence of turns — tool-call turns and a final-result turn —
identically on every run. That is what lets the whole build proceed at $0 (no
network, no key) and keeps the suite flake-free: same scenario, same outputs,
every time.

The scenario-authoring helpers (:func:`tool_call`, :func:`tool_turn`,
:func:`final_turn`, :func:`scenario`) build a turn sequence a test can read at a
glance; :class:`MockLLM` replays it one turn per ``complete()`` call.
"""

from __future__ import annotations

from typing import Any

from limpiador.agent.llm import LLMAdapter, Messages, ToolSchemas, register_adapter
from limpiador.schemas import LLMResponse, ToolCall

# The run-mode name the mock registers itself under. This string lives here, in
# the test-support layer — never in src/ — which is what lets LIMPIADOR_LLM=mock
# select the mock without production code ever naming it (.clauderules §5).
MOCK_MODE = "mock"
_UNCONFIGURED_TEXT = "(mock) no scenario configured — construct MockLLM with a scenario"


class MockExhaustedError(AssertionError):
    """The scenario was driven past its scripted turns — a test bug, not a result.

    Raised instead of improvising a reply, so an over-running loop fails loudly
    and deterministically rather than hanging or fabricating behavior.
    """


def tool_call(name: str, arguments: dict[str, Any] | None = None, *, call_id: str = "call_1") -> ToolCall:
    """One scripted tool call the mock will ask the loop to run."""
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def tool_turn(*tool_calls: ToolCall) -> LLMResponse:
    """A turn in which the model requests one or more tool calls (no text)."""
    return LLMResponse(text=None, tool_calls=list(tool_calls))


def final_turn(text: str) -> LLMResponse:
    """A terminal turn: the model returns a final result and no tool calls."""
    return LLMResponse(text=text, tool_calls=[])


def scenario(*turns: LLMResponse) -> list[LLMResponse]:
    """Author a scenario: the ordered turns the mock will replay."""
    return list(turns)


class MockLLM(LLMAdapter):
    """Replays a scripted scenario, one turn per ``complete()`` call, in order."""

    def __init__(self, turns: list[LLMResponse]) -> None:
        self._turns = list(turns)
        self._index = 0
        self.received: list[tuple[Messages, ToolSchemas | None]] = []

    def complete(self, messages: Messages, tools: ToolSchemas | None = None) -> LLMResponse:
        self.received.append((messages, tools))
        if self._index >= len(self._turns):
            raise MockExhaustedError(
                f"scenario has {len(self._turns)} turn(s) but complete() was called "
                f"{self._index + 1} time(s)."
            )
        response = self._turns[self._index]
        self._index += 1
        return response


def _build_default_mock() -> MockLLM:
    """Factory used when ``LIMPIADOR_LLM=mock`` selects the mock via build_adapter.

    Returns a mock with a trivial one-turn scenario so a bare ``mock`` run (e.g.
    ``make dev-mock``) is harmless; tests that need a specific script construct
    :class:`MockLLM` directly (or inject it), which is the common path.
    """
    return MockLLM(scenario(final_turn(_UNCONFIGURED_TEXT)))


# Make LIMPIADOR_LLM=mock selectable. Importing this module (the test-support
# layer does so via its package __init__, and conftest imports the package)
# registers the mock; production never executes this line.
register_adapter(MOCK_MODE, _build_default_mock)

"""Unit tests for the OpenAI adapter (ARCHITECTURE.md §10, agent/llm.py).

The adapter is the one place the openai SDK is allowed to live (.clauderules §5).
These tests drive it with a *stubbed* client — no network, no key — and assert
it normalizes the provider response into a typed LLMResponse: text, parallel
tool calls parsed to typed ToolCalls, and usage mapped to TokenUsage. A missing
key is a typed ConfigError, not a crash. A grep guard pins the SDK import to the
adapter alone.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from limpiador.agent.llm import LLMAdapter, OpenAIAdapter
from limpiador.observability.errors import ConfigError
from limpiador.schemas import LLMResponse, ToolCall

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "limpiador"


def _tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """Build a stub that mimics one openai tool_call object."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _response(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    usage: tuple[int, int] | None = (0, 0),
) -> SimpleNamespace:
    """Build a stub that mimics an openai ChatCompletion response object."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage_obj = None
    if usage is not None:
        usage_obj = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage_obj)


class _StubClient:
    """A duck-typed stand-in for openai.OpenAI that records the create() call."""

    def __init__(self, response: SimpleNamespace) -> None:
        self.create_kwargs: dict[str, Any] = {}
        completions = SimpleNamespace(create=self._make_create(response))
        self.chat = SimpleNamespace(completions=completions)

    def _make_create(self, response: SimpleNamespace):
        def create(**kwargs: Any) -> SimpleNamespace:
            self.create_kwargs = kwargs
            return response

        return create


def _adapter(response: SimpleNamespace) -> tuple[OpenAIAdapter, _StubClient]:
    client = _StubClient(response)
    return OpenAIAdapter(model="stub-model", client=client), client


def test_openai_adapter_is_an_llm_adapter() -> None:
    assert issubclass(OpenAIAdapter, LLMAdapter)


def test_complete_returns_a_typed_llm_response_with_text() -> None:
    adapter, _ = _adapter(_response(content="hello there", usage=(10, 3)))

    result = adapter.complete(messages=[{"role": "user", "content": "hi"}])

    assert isinstance(result, LLMResponse)
    assert result.text == "hello there"
    assert result.tool_calls == []


def test_complete_maps_usage_tokens() -> None:
    adapter, _ = _adapter(_response(content="ok", usage=(120, 8)))

    usage = adapter.complete(messages=[]).usage

    assert usage is not None
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 8
    assert usage.total_tokens == 128


def test_complete_parses_parallel_tool_calls() -> None:
    adapter, _ = _adapter(
        _response(
            tool_calls=[
                _tool_call("call_1", "git_status", "{}"),
                _tool_call("call_2", "ast_find_references", '{"file": "a.py", "symbol": "x"}'),
            ]
        )
    )

    calls = adapter.complete(messages=[]).tool_calls

    assert [c.id for c in calls] == ["call_1", "call_2"]
    assert all(isinstance(c, ToolCall) for c in calls)
    assert calls[0].name == "git_status"
    assert calls[0].arguments == {}
    assert calls[1].arguments == {"file": "a.py", "symbol": "x"}


def test_complete_handles_absent_tool_calls_and_usage() -> None:
    adapter, _ = _adapter(_response(content=None, tool_calls=None, usage=None))

    result = adapter.complete(messages=[])

    assert result.tool_calls == []
    assert result.usage is None


def test_complete_passes_flat_function_tool_array_through() -> None:
    adapter, client = _adapter(_response(content="ok"))
    tools = [{"type": "function", "function": {"name": "git_status", "parameters": {}}}]

    adapter.complete(messages=[{"role": "user", "content": "go"}], tools=tools)

    assert client.create_kwargs["model"] == "stub-model"
    assert client.create_kwargs["tools"] == tools


def test_complete_omits_tools_when_none_are_active() -> None:
    adapter, client = _adapter(_response(content="ok"))

    adapter.complete(messages=[{"role": "user", "content": "go"}])

    assert "tools" not in client.create_kwargs


def test_missing_api_key_raises_config_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ConfigError):
        OpenAIAdapter()  # no injected client, no key available


def test_openai_sdk_is_imported_only_in_the_adapter() -> None:
    """`.clauderules` §5: provider-specific code stays in agent/llm.py."""
    offenders = [
        path.relative_to(_SRC_ROOT).as_posix()
        for path in _SRC_ROOT.rglob("*.py")
        if "import openai" in path.read_text() or "from openai" in path.read_text()
    ]
    assert offenders == ["agent/llm.py"]

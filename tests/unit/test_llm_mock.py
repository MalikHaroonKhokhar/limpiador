"""Unit tests for the deterministic mock LLM (ARCHITECTURE.md §10-11, .clauderules §5).

The mock is the test infrastructure that lets the whole build proceed at $0 and
keeps the suite flake-free. These tests prove it: a scripted scenario replays
exactly and deterministically, the scenario helper produces valid LLMResponses,
selection honors LIMPIADOR_LLM, and — crucially — the mock is never referenced
under src/limpiador/ (it is injected through the LLMAdapter interface, not
branched on in production code).
"""

from __future__ import annotations

import pathlib

import pytest
from support.mock_llm import (
    MockExhaustedError,
    MockLLM,
    final_turn,
    scenario,
    tool_call,
    tool_turn,
)

from limpiador.agent.llm import LLMAdapter, OpenAIAdapter, build_adapter
from limpiador.observability.errors import ConfigError
from limpiador.schemas import LLMResponse, ToolCall

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "limpiador"


def _three_step_scenario() -> list[LLMResponse]:
    """status → find_references → final result: the canonical 3-step script."""
    return scenario(
        tool_turn(tool_call("git.status")),
        tool_turn(
            tool_call(
                "ast.find_references",
                {"file": "billing.py", "symbol": "calculate_total"},
            )
        ),
        final_turn("all done"),
    )


def test_mock_is_an_llm_adapter() -> None:
    assert issubclass(MockLLM, LLMAdapter)


def test_scripted_three_step_scenario_yields_exactly_those_calls() -> None:
    mock = MockLLM(_three_step_scenario())

    first = mock.complete(messages=[{"role": "user", "content": "go"}])
    second = mock.complete(messages=[])
    third = mock.complete(messages=[])

    assert [c.name for c in first.tool_calls] == ["git.status"]
    assert [c.name for c in second.tool_calls] == ["ast.find_references"]
    assert second.tool_calls[0].arguments == {
        "file": "billing.py",
        "symbol": "calculate_total",
    }
    assert third.tool_calls == []
    assert third.text == "all done"


def test_same_scenario_is_deterministic() -> None:
    """Same input → same output: two fresh runs of one script are identical."""
    runs = []
    for _ in range(2):
        mock = MockLLM(_three_step_scenario())
        runs.append([mock.complete(messages=[{"role": "user", "content": "go"}]) for _ in range(3)])

    assert runs[0] == runs[1]


def test_running_past_the_script_raises_rather_than_improvising() -> None:
    mock = MockLLM(scenario(final_turn("done")))
    mock.complete(messages=[])

    with pytest.raises(MockExhaustedError):
        mock.complete(messages=[])


def test_mock_records_the_inputs_it_received() -> None:
    mock = MockLLM(_three_step_scenario())
    tools = [{"type": "function", "function": {"name": "git_status"}}]

    mock.complete(messages=[{"role": "user", "content": "hi"}], tools=tools)

    assert mock.received[0] == ([{"role": "user", "content": "hi"}], tools)


def test_returned_response_does_not_alias_the_stored_script() -> None:
    """Mutating a returned response cannot corrupt the scripted turn it came from."""
    mock = MockLLM(scenario(tool_turn(tool_call("git.status"))))

    returned = mock.complete(messages=[])
    returned.tool_calls.append(tool_call("fs.read_file"))  # frozen field, mutable list

    assert len(mock._turns[0].tool_calls) == 1


def test_received_history_is_isolated_from_later_caller_mutation() -> None:
    """Mutating the passed messages after the call cannot rewrite recorded history."""
    mock = MockLLM(scenario(final_turn("ok")))
    messages = [{"role": "user", "content": "hi"}]

    mock.complete(messages=messages)
    messages.append({"role": "user", "content": "surprise"})

    assert mock.received[0][0] == [{"role": "user", "content": "hi"}]


def test_helper_produces_a_valid_llm_response_sequence() -> None:
    turns = _three_step_scenario()

    assert len(turns) == 3
    for turn in turns:
        assert isinstance(turn, LLMResponse)
        assert type(turn).model_validate(turn.model_dump()) == turn  # round-trips → valid
    assert all(isinstance(call, ToolCall) for call in turns[0].tool_calls)


# ---- selection by LIMPIADOR_LLM (the registration seam) ---------------------
def test_injected_adapter_is_returned_unchanged() -> None:
    mock = MockLLM(scenario(final_turn("x")))
    assert build_adapter(mock) is mock


def test_mock_mode_selects_the_registered_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """LIMPIADOR_LLM=mock actually builds the mock (test-support registered it)."""
    monkeypatch.setenv("LIMPIADOR_LLM", "mock")
    assert isinstance(build_adapter(), MockLLM)


def test_mock_mode_is_unavailable_when_test_support_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate production: with the mock unregistered, mock mode is a ConfigError.

    Proves src has no built-in knowledge of the mock — clearing the registry
    entry is enough to make it unavailable, because nothing in src can build one.
    """
    from limpiador.agent import llm

    monkeypatch.setenv("LIMPIADOR_LLM", "mock")
    monkeypatch.delitem(llm._ADAPTER_REGISTRY, "mock", raising=False)
    with pytest.raises(ConfigError):
        build_adapter()


def test_openai_mode_builds_the_real_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIMPIADOR_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    assert isinstance(build_adapter(), OpenAIAdapter)


def test_unknown_mode_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIMPIADOR_LLM", "banana")
    with pytest.raises(ConfigError):
        build_adapter()


# ---- import-graph guard: the mock never leaks into production code -----------
def test_mock_is_not_referenced_anywhere_under_src() -> None:
    offenders = [
        path.relative_to(_SRC_ROOT).as_posix()
        for path in _SRC_ROOT.rglob("*.py")
        if "MockLLM" in path.read_text() or "mock_llm" in path.read_text()
    ]
    assert offenders == []

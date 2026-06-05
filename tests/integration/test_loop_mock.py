"""Mock-integration tests for the agent loop (ARCHITECTURE.md §6, .clauderules §6).

These drive the *whole turn cycle* — guard → assemble → model call → dispatch →
fold → repeat — against the deterministic mock model and a registry of working
stub tools. The loop is pure orchestration: it never branches on tool identity,
so the stubs here stand in for any tool. What is under test is the spine itself:

* a scripted multi-step scenario reaches the expected end state and returns the
  ``finish`` result;
* several tool calls returned in one turn are all dispatched and folded;
* a tool raising a typed ``ToolError`` is folded as a structured result and the
  run *continues* rather than crashing;
* the call-count guard aborts a runaway loop with a typed ``RunAborted`` result.

The stubs have trivial typed I/O on purpose — the loop's correctness must not
depend on any particular tool's schema.
"""

from __future__ import annotations

from support.mock_llm import MockLLM, final_turn, scenario, tool_call, tool_turn

from limpiador.agent.guard import CallGuard
from limpiador.agent.loop import RunResult, run
from limpiador.observability.errors import NotFoundError, ToolError
from limpiador.schemas import Schema
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry


# ---- trivial typed I/O for the stubs ----------------------------------------
class _StubIn(Schema):
    value: str | None = None


class _StubOut(Schema):
    tool: str
    echoed: str | None = None


def _stub_tool(
    name: str,
    *,
    calls: list[tuple[str, _StubIn]],
    error: ToolError | None = None,
) -> Tool:
    """Build a working stub tool that records each invocation it receives.

    Returns a canned ``_StubOut`` (so the loop has a real typed result to fold)
    or, when ``error`` is set, raises it — to exercise the recoverable-failure path.
    """

    def run(self: Tool, request: _StubIn) -> _StubOut:
        calls.append((name, request))
        if error is not None:
            raise error
        return _StubOut(tool=name, echoed=request.value)

    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {
            "name": name,
            "description": f"stub for {name}",
            "Input": _StubIn,
            "Output": _StubOut,
            "run": run,
        },
    )
    return cls()


def _registry_with(*tools: Tool) -> ToolRegistry:
    """A registry with the given tools registered *and loaded* (discovery is
    unit-tested elsewhere; here the loop just dispatches)."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _tool_names(result: RunResult) -> list[str]:
    return list(result.tool_calls)


# ---- the canonical scripted scenario ----------------------------------------
def test_scripted_scenario_drives_the_loop_to_its_end_state() -> None:
    """read → find_refs → rename → run_tests → finish, one tool call per turn."""
    calls: list[tuple[str, _StubIn]] = []
    registry = _registry_with(
        _stub_tool("fs.read_file", calls=calls),
        _stub_tool("ast.find_references", calls=calls),
        _stub_tool("ast.rename_symbol", calls=calls),
        _stub_tool("test.run_tests", calls=calls),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "billing.py"})),
            tool_turn(tool_call("ast_find_references", {"value": "calculate_total"})),
            tool_turn(tool_call("ast_rename_symbol", {"value": "compute_total"})),
            tool_turn(tool_call("test_run_tests")),
            tool_turn(tool_call("finish", {"result": "renamed; tests green"})),
        )
    )

    result = run("rename calculate_total and keep tests green", registry=registry, adapter=mock)

    assert isinstance(result, RunResult)
    assert result.aborted is False
    assert result.result == "renamed; tests green"
    assert result.turns == 5
    assert _tool_names(result) == [
        "fs_read_file",
        "ast_find_references",
        "ast_rename_symbol",
        "test_run_tests",
        "finish",
    ]
    # the typed arguments reached each stub's run() in order
    assert [name for name, _ in calls] == [
        "fs.read_file",
        "ast.find_references",
        "ast.rename_symbol",
        "test.run_tests",
    ]
    assert calls[0][1].value == "billing.py"


# ---- parallel tool calls in a single turn -----------------------------------
def test_multiple_parallel_calls_in_one_turn_are_all_dispatched() -> None:
    calls: list[tuple[str, _StubIn]] = []
    registry = _registry_with(
        _stub_tool("fs.read_file", calls=calls),
        _stub_tool("ast.find_references", calls=calls),
    )
    mock = MockLLM(
        scenario(
            tool_turn(
                tool_call("fs_read_file", {"value": "a.py"}, call_id="call_a"),
                tool_call("ast_find_references", {"value": "sym"}, call_id="call_b"),
            ),
            tool_turn(tool_call("finish", {"result": "done"})),
        )
    )

    result = run("investigate", registry=registry, adapter=mock)

    assert result.result == "done"
    assert result.turns == 2  # both calls happened in turn 1
    assert {name for name, _ in calls} == {"fs.read_file", "ast.find_references"}
    assert _tool_names(result) == ["fs_read_file", "ast_find_references", "finish"]


# ---- a ToolError is folded and the run continues ----------------------------
def test_a_tool_error_is_folded_and_the_run_continues() -> None:
    calls: list[tuple[str, _StubIn]] = []
    registry = _registry_with(
        _stub_tool("fs.read_file", calls=calls, error=NotFoundError("no such file: ghost.py")),
        _stub_tool("ast.find_references", calls=calls),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "ghost.py"}, call_id="boom")),
            tool_turn(tool_call("ast_find_references", {"value": "sym"})),
            tool_turn(tool_call("finish", {"result": "recovered"})),
        )
    )

    result = run("be resilient", registry=registry, adapter=mock)

    # the run did not crash — it reached finish after the failed call
    assert result.result == "recovered"
    assert result.turns == 3
    assert _tool_names(result) == ["fs_read_file", "ast_find_references", "finish"]
    # the failure was folded back as a structured tool result the model could read
    folded = [m for m in result.messages if m.get("role") == "tool" and m.get("tool_call_id") == "boom"]
    assert folded, "the failed call should have produced a folded tool message"
    assert "NotFoundError" in folded[0]["content"]
    assert "ghost.py" in folded[0]["content"]


def test_the_loop_does_not_raise_when_a_tool_fails() -> None:
    calls: list[tuple[str, _StubIn]] = []
    registry = _registry_with(
        _stub_tool("fs.read_file", calls=calls, error=ToolError("transient hiccup")),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "x"})),
            tool_turn(tool_call("finish", {"result": "ok"})),
        )
    )
    # no exception escapes the loop
    assert run("t", registry=registry, adapter=mock).result == "ok"


# ---- the guard aborts a runaway loop ----------------------------------------
def test_the_loop_aborts_when_the_guard_ceiling_is_reached() -> None:
    calls: list[tuple[str, _StubIn]] = []
    registry = _registry_with(_stub_tool("fs.read_file", calls=calls))
    # a scenario that never calls finish — exactly enough turns to hit the ceiling
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "1"})),
            tool_turn(tool_call("fs_read_file", {"value": "2"})),
        )
    )

    result = run("loop forever", registry=registry, adapter=mock, guard=CallGuard(ceiling=2))

    assert result.aborted is True
    assert result.result is None
    assert len(calls) == 2  # it ran right up to the ceiling, no further

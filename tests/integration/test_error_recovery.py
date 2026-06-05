"""A tool failure is recoverable, end to end (CLEAN_CODE.md §6, ARCHITECTURE.md §6).

Property 4 in action through the loop: when a tool raises a typed ``ToolError``,
the loop folds its ``as_tool_result()`` back into the transcript as a structured,
model-readable result — the model reads "file not found", adapts, and the run
continues to ``finish`` instead of crashing. The trace records the failure too,
so nothing is silently swallowed.
"""

from __future__ import annotations

import json

from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.agent.loop import run
from limpiador.observability.errors import NotFoundError, ToolError
from limpiador.schemas import Schema
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry


class _In(Schema):
    value: str | None = None


class _Out(Schema):
    tool: str


def _tool(name: str, *, error: ToolError | None = None) -> Tool:
    def run_(self: Tool, request: _In) -> _Out:
        if error is not None:
            raise error
        return _Out(tool=name)

    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {"name": name, "description": f"stub {name}", "Input": _In, "Output": _Out, "run": run_},
    )
    return cls()


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _recovery_run():
    registry = _registry(
        _tool("fs.read_file", error=NotFoundError("no such file: ghost.py")),
        _tool("ast.find_references"),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "ghost.py"}, call_id="boom")),
            tool_turn(tool_call("ast_find_references", {"value": "calc"})),
            tool_turn(tool_call("finish", {"result": "recovered without the missing file"})),
        )
    )
    return run("be resilient", registry=registry, adapter=mock)


def test_a_tool_error_lets_the_agent_recover_on_the_next_turn() -> None:
    result = _recovery_run()

    # the run did not crash on the failed call — it reached finish
    assert result.aborted is False
    assert result.result == "recovered without the missing file"
    assert result.tool_calls == ("fs_read_file", "ast_find_references", "finish")


def test_the_failure_is_folded_as_a_structured_recoverable_result() -> None:
    result = _recovery_run()

    folded = next(
        m for m in result.messages if m.get("role") == "tool" and m.get("tool_call_id") == "boom"
    )
    payload = json.loads(str(folded["content"]))
    # exactly the as_tool_result() shape — readable, not a stack trace
    assert payload["error"] == "NotFoundError"
    assert "ghost.py" in payload["message"]
    assert payload["recoverable"] is True


def test_the_failure_is_recorded_in_the_trace_not_swallowed() -> None:
    result = _recovery_run()

    failed = next(e for e in result.trace.tool_calls if e.name == "fs_read_file")
    assert failed.error == "NotFoundError"

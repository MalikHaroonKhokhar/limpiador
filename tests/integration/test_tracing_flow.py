"""The trace produced by a real loop run (ARCHITECTURE.md §6, §13).

`test_tracing.py` exercises the Tracer mechanism directly; this drives the *whole
loop* with the deterministic mock adapter and asserts the trace it leaves behind
is the substrate the eval harness will assert against: one structured entry per
scripted model turn and per dispatched tool call, in order, with the eval helpers
(`called`, `order`, `call_count`) returning the right answers. A failing tool
call still leaves a recorded entry — the trace tells the whole story, including
the recoverable error.
"""

from __future__ import annotations

from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.agent.loop import run
from limpiador.observability.errors import NotFoundError, ToolError
from limpiador.observability.tracing import Tracer
from limpiador.schemas import Schema
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry


class _StubIn(Schema):
    value: str | None = None


class _StubOut(Schema):
    tool: str


def _stub_tool(name: str, *, error: ToolError | None = None) -> Tool:
    def run_(self: Tool, request: _StubIn) -> _StubOut:
        if error is not None:
            raise error
        return _StubOut(tool=name)

    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {"name": name, "description": f"stub {name}", "Input": _StubIn, "Output": _StubOut, "run": run_},
    )
    return cls()


def _loaded_registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _canonical_run() -> Tracer:
    registry = _loaded_registry(
        _stub_tool("fs.read_file"),
        _stub_tool("ast.find_references"),
        _stub_tool("ast.rename_symbol"),
        _stub_tool("test.run_tests"),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "billing.py"})),
            tool_turn(tool_call("ast_find_references", {"value": "calc"})),
            tool_turn(tool_call("ast_rename_symbol", {"value": "compute"})),
            tool_turn(tool_call("test_run_tests")),
            tool_turn(tool_call("finish", {"result": "done"})),
        )
    )
    result = run("rename calc and keep tests green", registry=registry, adapter=mock)
    assert result.trace is not None
    return result.trace


def test_a_scripted_run_leaves_one_entry_per_model_turn_and_tool_call() -> None:
    trace = _canonical_run()

    # five scripted turns → five model calls; five tool calls dispatched in them
    assert len(trace.model_calls) == 5
    assert trace.call_count() == 5
    assert [e.name for e in trace.tool_calls] == [
        "fs_read_file",
        "ast_find_references",
        "ast_rename_symbol",
        "test_run_tests",
        "finish",
    ]


def test_the_eval_helpers_answer_correctly_off_the_run_trace() -> None:
    trace = _canonical_run()

    assert trace.called("ast_rename_symbol") is True
    assert trace.called("git_status") is False
    # the agent found references before it renamed — the order the eval asserts
    assert trace.order("ast_find_references", "ast_rename_symbol") is True
    assert trace.order("ast_rename_symbol", "ast_find_references") is False
    assert trace.call_count("fs_read_file") == 1


def test_a_tool_input_and_output_are_captured_on_the_entry() -> None:
    trace = _canonical_run()
    read = next(e for e in trace.tool_calls if e.name == "fs_read_file")
    assert read.input == {"value": "billing.py"}
    assert read.output is not None  # the folded typed result is recorded


def test_a_failed_tool_call_is_still_recorded_with_its_error() -> None:
    registry = _loaded_registry(
        _stub_tool("fs.read_file", error=NotFoundError("no such file: ghost.py")),
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "ghost.py"})),
            tool_turn(tool_call("finish", {"result": "recovered"})),
        )
    )

    result = run("be resilient", registry=registry, adapter=mock)

    assert result.result == "recovered"
    failed = next(e for e in result.trace.tool_calls if e.name == "fs_read_file")
    assert failed.error == "NotFoundError"


def test_a_caller_supplied_tracer_is_used() -> None:
    registry = _loaded_registry(_stub_tool("fs.read_file"))
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "x"})),
            tool_turn(tool_call("finish", {"result": "ok"})),
        )
    )
    tracer = Tracer()

    result = run("t", registry=registry, adapter=mock, tracer=tracer)

    assert result.trace is tracer  # the loop recorded into the injected tracer
    assert tracer.called("fs_read_file")

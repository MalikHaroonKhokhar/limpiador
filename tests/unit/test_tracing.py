"""Unit tests for structured tracing and token/cost accounting (§13).

The trace is the substrate the eval harness asserts against: every model call and
every tool call is recorded as a *structured object* (tool, input, output,
latency, tokens, routing decision) — never a log string a test would have to grep.
These tests pin that contract and the eval-harness helpers built on top of it:

* ``called(tool)`` / ``order(a, b)`` / ``call_count()`` answer "did it run, in
  what order, how many times" off a known trace;
* token totals sum across model calls and per-run cost adds up;
* tagged events (the debt/observability tags) are captured, and a Tracer is a
  drop-in for the ``(tag, message)`` tracer seam the adapter already uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from limpiador.agent.llm import DEFAULT_ROUTING, OpenAIAdapter
from limpiador.observability.tracing import (
    ROUTING_TAG,
    CallKind,
    TraceEntry,
    Tracer,
)
from limpiador.schemas import TokenUsage


def _usage(prompt: int, completion: int) -> TokenUsage:
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion)


# ---- structured recording ---------------------------------------------------
def test_model_and_tool_calls_are_recorded_as_structured_objects() -> None:
    tracer = Tracer()
    tracer.record_model_call(model="gpt-4o-mini", latency_s=0.2, usage=_usage(100, 20), route="dispatch")
    tracer.record_tool_call(tool="fs.read_file", latency_s=0.01, input={"path": "a.py"}, output="...")

    assert [e.kind for e in tracer.entries] == [CallKind.MODEL, CallKind.TOOL]
    model_entry = tracer.model_calls[0]
    assert isinstance(model_entry, TraceEntry)
    assert model_entry.name == "gpt-4o-mini"
    assert model_entry.route == "dispatch"
    assert model_entry.usage is not None and model_entry.usage.total_tokens == 120
    tool_entry = tracer.tool_calls[0]
    assert tool_entry.name == "fs.read_file"
    assert tool_entry.input == {"path": "a.py"}


def test_a_failed_tool_call_records_its_error_kind() -> None:
    tracer = Tracer()
    tracer.record_tool_call(tool="fs.read_file", latency_s=0.0, error="NotFoundError")
    assert tracer.tool_calls[0].error == "NotFoundError"


# ---- eval-harness helpers on a known trace ----------------------------------
def _known_trace() -> Tracer:
    tracer = Tracer()
    for tool in ("fs.read_file", "ast.find_references", "ast.rename_symbol", "fs.read_file"):
        tracer.record_tool_call(tool=tool, latency_s=0.0)
    return tracer


def test_called_reports_whether_a_tool_ran() -> None:
    tracer = _known_trace()
    assert tracer.called("ast.rename_symbol") is True
    assert tracer.called("git.status") is False


def test_order_reflects_first_occurrence_of_each_tool() -> None:
    tracer = _known_trace()
    assert tracer.order("fs.read_file", "ast.rename_symbol") is True
    assert tracer.order("ast.rename_symbol", "fs.read_file") is False
    # a tool that never ran cannot be ordered
    assert tracer.order("git.status", "fs.read_file") is False


def test_call_count_totals_and_filters_by_name() -> None:
    tracer = _known_trace()
    assert tracer.call_count() == 4  # total tool calls
    assert tracer.call_count("fs.read_file") == 2
    assert tracer.call_count("git.status") == 0


# ---- token / cost accounting ------------------------------------------------
def test_token_totals_sum_across_model_calls() -> None:
    tracer = Tracer()
    tracer.record_model_call(model="m", latency_s=0.0, usage=_usage(100, 10))
    tracer.record_model_call(model="m", latency_s=0.0, usage=_usage(50, 5))
    tracer.record_model_call(model="m", latency_s=0.0, usage=None)  # a usage-less call

    assert tracer.total_prompt_tokens() == 150
    assert tracer.total_completion_tokens() == 15
    assert tracer.total_tokens() == 165


def test_per_run_cost_sums_recorded_call_costs() -> None:
    tracer = Tracer()
    tracer.record_model_call(model="strong", latency_s=0.0, usage=_usage(1000, 100), cost_usd=0.0035)
    tracer.record_model_call(model="cheap", latency_s=0.0, usage=_usage(2000, 200), cost_usd=0.00042)
    tracer.record_model_call(model="mock", latency_s=0.0, usage=None)  # unpriced → ignored

    assert abs(tracer.total_cost_usd() - 0.00392) < 1e-9


# ---- tags -------------------------------------------------------------------
def test_tags_are_captured_and_countable() -> None:
    tracer = Tracer()
    tracer.tag(ROUTING_TAG, "dispatch -> cheap")
    tracer(ROUTING_TAG, "dispatch -> cheap")  # callable seam: (tag, message)
    tracer.tag("[CONTEXT REREAD]", "billing.py")

    assert tracer.has_tag(ROUTING_TAG)
    assert tracer.count_tag(ROUTING_TAG) == 2
    assert ("[CONTEXT REREAD]", "billing.py") in tracer.tags


# ---- the Tracer is a drop-in for the existing (tag, message) seam ------------
class _StubClient:
    def __init__(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: response)
        )


def test_a_tracer_instance_captures_the_adapters_routing_tag() -> None:
    tracer = Tracer()
    adapter = OpenAIAdapter(client=_StubClient(), tracer=tracer)

    # a dispatch-shaped turn → the adapter emits its routing tag through the tracer
    adapter.complete(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "c1", "name": "x", "content": "r"},
        ]
    )

    assert tracer.has_tag(ROUTING_TAG)
    assert DEFAULT_ROUTING.cheap.name in tracer.tags[0][1]

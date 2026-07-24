"""A single 20+ call session that keeps its plan (HAR-33, ARCHITECTURE.md §7).

This is the end-to-end proof of property #3. The mechanism (working memory,
summarize-then-evict, durable sub-goals) was built in HAR-12 and is exercised
directly in ``test_compaction_flow.py``; here it is driven *through the whole
loop* — the same ``run()`` production takes — so the claim being tested is the
orchestration one:

    a session can span twenty-plus tool calls, cross the compaction threshold
    repeatedly, and still finish with its plan intact and no resolved sub-goal
    re-litigated.

The adapter is the deterministic mock, so the session is reproducible and costs
nothing; the *real-model* counterpart of this session lives in
``tests/reproduce/test_long_session_stays_coherent.py``.

The four assertions are exactly the ticket's acceptance:

* the session really is long — ``call_count >= 20``;
* the goal was reached — the loop finished rather than aborting on the ceiling;
* compaction actually fired mid-flight, and is *visible in the trace*;
* the plan stayed coherent — every declared sub-goal survived, the ones the run
  resolved are still resolved, and nothing was re-litigated.
"""

from __future__ import annotations

from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.agent.loop import run
from limpiador.observability.tracing import COMPACTION_TAG
from limpiador.schemas import Schema
from limpiador.tools.base import Tool
from limpiador.tools.registry import PLAN_ADD, PLAN_RESOLVE, ToolRegistry

# The plan the session declares up front, then works through.
_PLAN = (
    "map the call sites of calculate_total",
    "update every call site",
    "verify the suite is green",
)

# Each read is large relative to the threshold, so a handful of them trips
# compaction — the same shape a real long run hits, just sooner.
_THRESHOLD = 900
_BIG = "x" * (400 * 4)


class _ReadIn(Schema):
    value: str | None = None


class _ReadOut(Schema):
    contents: str | None = None


def _echo_tool(name: str) -> Tool:
    """A stub that echoes its (large) input back as a typed result to fold."""

    def run_(self: Tool, request: _ReadIn) -> _ReadOut:
        return _ReadOut(contents=request.value)

    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {
            "name": name,
            "description": f"stub {name}",
            "Input": _ReadIn,
            "Output": _ReadOut,
            "run": run_,
        },
    )
    return cls()


def _loaded_registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _long_session() -> MockLLM:
    """Script a session of 20+ calls: declare a plan, investigate, resolve each
    milestone as it is reached, then finish."""
    turns = [tool_turn(tool_call(PLAN_ADD, {"sub_goals": list(_PLAN)}, call_id="plan"))]

    # 18 large reads, resolving a milestone at three points along the way.
    milestones = {5: _PLAN[0], 11: _PLAN[1], 17: _PLAN[2]}
    for i in range(18):
        turns.append(tool_turn(tool_call("fs_read_file", {"value": _BIG}, call_id=f"r{i}")))
        if i in milestones:
            turns.append(
                tool_turn(
                    tool_call(PLAN_RESOLVE, {"sub_goal": milestones[i]}, call_id=f"done{i}")
                )
            )

    turns.append(tool_turn(tool_call("finish", {"result": "all call sites updated"})))
    return MockLLM(scenario(*turns))


def _run_session():
    return run(
        "rename calculate_total across the billing package",
        registry=_loaded_registry(_echo_tool("fs.read_file")),
        adapter=_long_session(),
        threshold_tokens=_THRESHOLD,
    )


# ---- the session is genuinely long and reaches its goal ----------------------
def test_the_session_spans_twenty_plus_calls_and_reaches_the_goal() -> None:
    result = _run_session()

    assert len(result.tool_calls) >= 20, "the session must span 20+ tool calls"
    assert result.aborted is False, "the run must reach its goal, not hit the ceiling"
    assert result.result == "all call sites updated"


# ---- compaction fired, and it is visible in the trace -----------------------
def test_compaction_fires_and_is_visible_in_the_trace() -> None:
    result = _run_session()
    assert result.trace is not None

    assert result.trace.count_tag(COMPACTION_TAG) >= 1, (
        "a 20+ call session over the threshold must compact, and say so in the trace"
    )
    # mid-flight, not a closing sweep: earlier reads were summarized out of the
    # transcript while the run was still going.
    reads = [m for m in result.messages if m.get("name") == "fs_read_file"]
    assert any("elided after compaction" in str(m["content"]) for m in reads)


# ---- the plan survived: durable state is intact across every compaction -----
def test_the_plan_survives_the_whole_session() -> None:
    result = _run_session()
    context = result.context
    assert context is not None

    # every declared sub-goal is still on the plan after 20+ calls of eviction...
    assert {g.description for g in context.sub_goals} == set(_PLAN)
    # ...and the milestones the run reached are still marked resolved.
    assert {g.description for g in context.resolved_sub_goals} == set(_PLAN)
    assert context.unresolved_sub_goals == ()


def test_no_resolved_sub_goal_is_re_litigated() -> None:
    """Plan coherence: the session never re-opens work it already closed."""
    result = _run_session()

    resolved: set[str] = set()
    for call in result.protocol_calls:
        if call.name != PLAN_RESOLVE:
            continue
        goal = str(call.arguments.get("sub_goal", ""))
        assert goal not in resolved, f"sub-goal re-litigated after resolution: {goal!r}"
        resolved.add(goal)

    assert resolved == set(_PLAN)

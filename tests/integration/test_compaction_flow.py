"""Compaction over a long, scripted investigation (ARCHITECTURE.md §7).

This is the flow-level test for property #3: a twenty-plus-step run drives the
Context the way the loop will — record a plan, then read file after file, noting
symbol facts and resolving sub-goals as it goes, compacting whenever the
estimated footprint crosses the threshold. The script is deterministic (no model,
no network), exactly like a mock run, and the assertions are the invariants a
long run must hold:

* compaction fires *mid-flight*, not only at the end;
* the footprint never runs away — it stays near the threshold the whole time;
* the plan survives and no resolved sub-goal is ever re-litigated;
* symbol facts noted early remain available late, even after their raw reads are
  evicted (the ARCH_DEBT_002 mitigation in action).
"""

from __future__ import annotations

from dataclasses import dataclass

from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.agent.context import (
    Context,
    SymbolFact,
    SymbolKind,
)
from limpiador.agent.loop import run
from limpiador.schemas import Schema
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry


@dataclass(frozen=True)
class _Step:
    """One scripted step of the investigation: read a file, learn a fact."""

    path: str
    symbol: str
    location: str
    resolves: str | None = None


# A scripted ~20-step investigation: walk the module, learning one durable fact
# per read, resolving a planned sub-goal at three milestones along the way.
_SCRIPT: tuple[_Step, ...] = (
    _Step("billing.py", "calculate_total", "billing.py:40", resolves="map the call graph"),
    _Step("checkout.py", "checkout", "checkout.py:12"),
    _Step("report.py", "build_report", "report.py:88"),
    _Step("invoice.py", "render_invoice", "invoice.py:5"),
    _Step("tax.py", "apply_tax", "tax.py:21"),
    _Step("ledger.py", "post_entry", "ledger.py:7", resolves="find the rename targets"),
    _Step("account.py", "Account", "account.py:3"),
    _Step("currency.py", "convert", "currency.py:60"),
    _Step("discount.py", "apply_discount", "discount.py:14"),
    _Step("refund.py", "issue_refund", "refund.py:30"),
    _Step("audit.py", "log_event", "audit.py:9"),
    _Step("notify.py", "send_receipt", "notify.py:42"),
    _Step("cart.py", "Cart", "cart.py:2"),
    _Step("pricing.py", "price_for", "pricing.py:18"),
    _Step("shipping.py", "shipping_cost", "shipping.py:25"),
    _Step("coupon.py", "redeem", "coupon.py:11"),
    _Step("wallet.py", "Wallet", "wallet.py:4"),
    _Step("settlement.py", "settle", "settlement.py:50"),
    _Step("fees.py", "compute_fee", "fees.py:33"),
    _Step("export.py", "to_csv", "export.py:77", resolves="confirm nothing else references it"),
)

_PLAN = (
    "map the call graph",
    "find the rename targets",
    "confirm nothing else references it",
)

# A small threshold so a handful of raw reads trips compaction — the same shape
# a real run hits, just sooner.
_THRESHOLD = 700
# Each raw file read is large relative to the threshold, so a few reads blow it.
_RAW_TOKENS = 300


def _read_payload(path: str) -> str:
    return f"# {path}\n" + "raw source line\n" * (_RAW_TOKENS * 4 // 16)


def _drive_the_run() -> tuple[Context, list[int], int]:
    """Replay the script, compacting on threshold. Returns the context, the
    footprint sampled after every step, and how many times compaction fired."""
    ctx = Context("audit and rename across the billing package", threshold_tokens=_THRESHOLD)
    for goal in _PLAN:
        ctx.add_sub_goal(goal)

    footprints: list[int] = []
    compactions = 0
    for step in _SCRIPT:
        ctx.record_payload(step.path, _read_payload(step.path))
        ctx.record_symbol_fact(SymbolFact(step.symbol, SymbolKind.DEFINITION, step.location))
        if step.resolves is not None:
            ctx.resolve_sub_goal(step.resolves)

        # Threshold-triggered, not per-call: only compact when over budget.
        if ctx.estimated_tokens() > _THRESHOLD:
            result = ctx.compact()
            if result.evicted:
                compactions += 1
        footprints.append(ctx.estimated_tokens())

    return ctx, footprints, compactions


def test_compaction_fires_mid_flight_not_just_at_the_end() -> None:
    _ctx, footprints, compactions = _drive_the_run()

    assert compactions >= 1, "a 20-read run must trip compaction"
    # mid-flight: the footprint dipped at least once *before* the final step,
    # proving eviction happened during the run, not as a closing sweep.
    dipped_early = any(
        footprints[i] < footprints[i - 1] for i in range(1, len(footprints) - 1)
    )
    assert dipped_early


def test_the_footprint_stays_flat_across_a_long_run() -> None:
    _ctx, footprints, _compactions = _drive_the_run()

    # "roughly flat": the peak footprint never runs far past the threshold — it is
    # bounded by threshold + one in-flight raw read, not by the 20-read total.
    assert max(footprints) <= _THRESHOLD + _RAW_TOKENS + 100
    # and the final state is back under the threshold
    assert footprints[-1] <= _THRESHOLD


def test_the_plan_survives_and_no_resolved_sub_goal_is_re_litigated() -> None:
    ctx, _footprints, _compactions = _drive_the_run()

    # the whole plan is still present after a run full of compactions
    assert {g.description for g in ctx.sub_goals} == set(_PLAN)
    # every milestone the run resolved is still marked resolved — the agent would
    # never redo them, because their resolution lives in durable state
    assert {g.description for g in ctx.resolved_sub_goals} == set(_PLAN)
    assert ctx.unresolved_sub_goals == ()


def test_symbol_facts_from_early_reads_outlive_their_raw_payloads() -> None:
    ctx, _footprints, _compactions = _drive_the_run()

    # the import edge / definition learned on call 1 is still here on call 20...
    learned = {f.symbol for f in ctx.symbol_facts}
    assert {step.symbol for step in _SCRIPT} <= learned
    # ...even though the raw read that produced the earliest fact is long evicted
    first = next(p for p in ctx.payloads if p.key == _SCRIPT[0].path)
    assert first.evicted


# ============================================================================
# Through the orchestration: a scripted mock run that compacts mid-flight.
#
# The tests above drive the Context directly; these drive the *whole loop* with
# the deterministic mock adapter — the same path `make dev-mock` takes — proving
# compaction is wired into run(), not just available as a mechanism. A stub tool
# returns a large payload each call, so a few turns trip the threshold and the
# loop summarizes earlier results out of the transcript before reaching finish.
# ============================================================================


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
        {"name": name, "description": f"stub {name}", "Input": _ReadIn, "Output": _ReadOut, "run": run_},
    )
    return cls()


def _loaded_registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _big_read(token_estimate: int = 400) -> str:
    return "x" * (token_estimate * 4)


def test_a_scripted_mock_run_compacts_the_transcript_mid_flight() -> None:
    registry = _loaded_registry(_echo_tool("fs.read_file"))
    big = _big_read()
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": big}, call_id="c1")),
            tool_turn(tool_call("fs_read_file", {"value": big}, call_id="c2")),
            tool_turn(tool_call("fs_read_file", {"value": big}, call_id="c3")),
            tool_turn(tool_call("fs_read_file", {"value": big}, call_id="c4")),
            tool_turn(tool_call("finish", {"result": "investigated"})),
        )
    )

    # a threshold a couple of raw reads will cross, so compaction must fire mid-run
    result = run("read the whole module", registry=registry, adapter=mock, threshold_tokens=900)

    # the run still completed through the loop, despite mid-flight compaction
    assert result.aborted is False
    assert result.result == "investigated"

    reads = [m for m in result.messages if m.get("name") == "fs_read_file"]
    summarized = [m for m in reads if "elided after compaction" in str(m["content"])]
    still_raw = [m for m in reads if big in str(m["content"])]

    # earlier reads were summarized out of the transcript...
    assert summarized, "an earlier large result should have been evicted to a summary"
    # ...while the transcript stayed flat: only a small working set keeps its raw text
    assert len(still_raw) <= 2


def test_a_short_run_below_threshold_keeps_every_raw_result() -> None:
    """The default high threshold means a normal short run is never compacted."""
    registry = _loaded_registry(_echo_tool("fs.read_file"))
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"value": "small"}, call_id="c1")),
            tool_turn(tool_call("fs_read_file", {"value": "also small"}, call_id="c2")),
            tool_turn(tool_call("finish", {"result": "done"})),
        )
    )

    result = run("quick look", registry=registry, adapter=mock)

    assert result.result == "done"
    reads = [m for m in result.messages if m.get("name") == "fs_read_file"]
    assert all("elided after compaction" not in str(m["content"]) for m in reads)

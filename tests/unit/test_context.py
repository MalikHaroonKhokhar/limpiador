"""Unit tests for working memory and compaction (ARCHITECTURE.md §7, property #3).

The Context object is the *mechanism* that keeps a long-horizon run flat: durable
state (task, plan, resolved sub-goals, symbol facts) is never evicted, while
transient raw payloads (file/diff/log text) are summarized-then-evicted once they
are no longer the immediate next step. These tests pin that contract:

* durable state survives compaction; the raw bulk of a payload does not;
* a twenty-result history stays under the token threshold after compaction;
* symbol facts are never evicted, even under aggressive compaction;
* the most-recent payload and any pinned payload are protected;
* compaction below the threshold is a no-op;
* re-reading a file emits the ``[CONTEXT REREAD]`` debt tag (ARCH_DEBT_002).

The numbers assert *relationships* (footprint vs threshold, before vs after),
never exact token counts, so the estimator can be retuned without churn here.
"""

from __future__ import annotations

import pytest

from limpiador.agent.context import (
    DEFAULT_COMPACTION_THRESHOLD_TOKENS,
    Context,
    PayloadKind,
    SymbolFact,
    SymbolKind,
    estimate_tokens,
)
from limpiador.observability.tracing import CONTEXT_REREAD_TAG


def _big(label: str, *, tokens: int = 200) -> str:
    """A raw payload whose footprint is about ``tokens`` tokens."""
    return f"{label}:" + "x" * (tokens * 4)


def _recording_tracer() -> tuple[list[tuple[str, str]], "callable"]:
    events: list[tuple[str, str]] = []

    def trace(tag: str, message: str = "") -> None:
        events.append((tag, message))

    return events, trace


# ---- estimator --------------------------------------------------------------
def test_estimate_tokens_scales_with_text_length() -> None:
    assert estimate_tokens("") == 0
    short = estimate_tokens("a" * 40)
    long = estimate_tokens("a" * 400)
    assert long > short > 0


# ---- the headline property: durable survives, raw is evicted ----------------
def test_durable_state_survives_compaction_but_raw_payload_is_evicted() -> None:
    ctx = Context("rename calculate_total across billing", threshold_tokens=300)
    ctx.add_sub_goal("find every reference")
    ctx.resolve_sub_goal("find every reference")
    ctx.record_symbol_fact(
        SymbolFact("calculate_total", SymbolKind.DEFINITION, "billing.py:40")
    )
    ctx.record_payload("billing.py", _big("billing", tokens=400))
    ctx.record_payload("checkout.py", _big("checkout", tokens=400))
    ctx.record_payload("report.py", _big("report", tokens=400))

    before = ctx.estimated_tokens()
    result = ctx.compact()

    # compaction actually fired and shrank the footprint
    assert before > 300
    assert result.evicted >= 1
    assert ctx.estimated_tokens() < before

    # durable state is fully intact
    assert ctx.task == "rename calculate_total across billing"
    assert [g.description for g in ctx.resolved_sub_goals] == ["find every reference"]
    assert ctx.symbol_facts[0].symbol == "calculate_total"

    # the oldest raw payload is gone, replaced by a compact summary
    oldest = ctx.payloads[0]
    assert oldest.evicted
    assert oldest.raw is None
    assert oldest.summary  # a stand-in remains so the gist is not lost
    assert "billing.py" in oldest.summary


def test_the_most_recent_payload_is_protected_from_eviction() -> None:
    """The immediate next step keeps its raw text; only stale payloads are evicted."""
    ctx = Context("investigate", threshold_tokens=250)
    ctx.record_payload("a.py", _big("a", tokens=300))
    ctx.record_payload("b.py", _big("b", tokens=300))
    latest = ctx.record_payload("c.py", _big("c", tokens=300))

    ctx.compact()

    assert not latest.evicted
    assert latest.raw is not None
    assert ctx.payloads[0].evicted  # the stale ones gave up their raw text


# ---- flatness: a long history stays bounded ---------------------------------
def test_a_twenty_result_history_stays_under_threshold_after_compaction() -> None:
    threshold = 600
    ctx = Context("audit the module", threshold_tokens=threshold)
    for i in range(20):
        ctx.record_payload(f"file_{i:02d}.py", _big(f"file{i}", tokens=200))

    assert ctx.estimated_tokens() > threshold  # twenty raw reads blow the window

    result = ctx.compact()

    assert ctx.estimated_tokens() <= threshold  # flat again after compaction
    assert result.footprint_after < result.footprint_before
    # only the single most-recent raw payload remains live
    assert len([p for p in ctx.payloads if not p.evicted]) == 1
    assert result.evicted == 19


# ---- symbol facts are never evicted -----------------------------------------
def test_symbol_facts_are_never_evicted_even_under_heavy_compaction() -> None:
    ctx = Context("wide refactor", threshold_tokens=100)
    facts = [
        SymbolFact(f"sym_{i}", SymbolKind.IMPORT, f"mod_{i}.py:{i}", detail="edge")
        for i in range(15)
    ]
    for fact in facts:
        ctx.record_symbol_fact(fact)
    for i in range(10):
        ctx.record_payload(f"f_{i}.py", _big(f"f{i}", tokens=300))

    ctx.compact()

    # every symbol fact noted on an early call is still available on a late one
    assert list(ctx.symbol_facts) == facts
    # while the raw reads that produced them are gone
    assert any(p.evicted for p in ctx.payloads)


# ---- pinning protects a payload the caller still needs ----------------------
def test_a_pinned_payload_survives_compaction() -> None:
    ctx = Context("keep this one", threshold_tokens=250)
    ctx.record_payload("pinned.py", _big("pinned", tokens=300))
    ctx.record_payload("middle.py", _big("middle", tokens=300))
    ctx.record_payload("latest.py", _big("latest", tokens=300))
    ctx.pin("pinned.py")

    ctx.compact()

    pinned = next(p for p in ctx.payloads if p.key == "pinned.py")
    middle = next(p for p in ctx.payloads if p.key == "middle.py")
    assert not pinned.evicted  # explicitly protected
    assert middle.evicted  # unprotected and stale → evicted


# ---- the no-op edge ---------------------------------------------------------
def test_compaction_below_the_threshold_is_a_noop() -> None:
    ctx = Context("small task", threshold_tokens=DEFAULT_COMPACTION_THRESHOLD_TOKENS)
    ctx.record_payload("tiny.py", "a small file")
    before = ctx.estimated_tokens()

    result = ctx.compact()

    assert before <= DEFAULT_COMPACTION_THRESHOLD_TOKENS
    assert result.evicted == 0
    assert result.footprint_before == result.footprint_after == before
    assert not ctx.payloads[0].evicted  # nothing was touched


# ---- ARCH_DEBT_002: re-reading a file is observable -------------------------
def test_rereading_a_file_emits_the_context_reread_tag() -> None:
    events, trace = _recording_tracer()
    ctx = Context("touch a file twice", tracer=trace)

    ctx.record_payload("billing.py", _big("first"))
    assert not events  # first read is silent

    ctx.record_payload("billing.py", _big("second"))

    assert (CONTEXT_REREAD_TAG, "billing.py") in events


def test_a_non_file_payload_does_not_count_as_a_reread() -> None:
    events, trace = _recording_tracer()
    ctx = Context("logs are not files", tracer=trace)

    ctx.record_payload("pytest", _big("run1"), kind=PayloadKind.LOG)
    ctx.record_payload("pytest", _big("run2"), kind=PayloadKind.LOG)

    assert events == []  # re-running a check is not a file reread


# ---- resolved sub-goals are durable -----------------------------------------
def test_resolved_sub_goals_are_retained_through_compaction() -> None:
    ctx = Context("multi-step task", threshold_tokens=200)
    ctx.add_sub_goal("locate the symbol")
    ctx.add_sub_goal("rename it")
    ctx.add_sub_goal("run the tests")
    ctx.resolve_sub_goal("locate the symbol")
    ctx.resolve_sub_goal("rename it")
    for i in range(8):
        ctx.record_payload(f"f{i}.py", _big(f"f{i}", tokens=200))

    ctx.compact()

    resolved = {g.description for g in ctx.resolved_sub_goals}
    assert resolved == {"locate the symbol", "rename it"}
    assert [g.description for g in ctx.unresolved_sub_goals] == ["run the tests"]


def test_resolving_an_unknown_sub_goal_records_it_as_resolved() -> None:
    ctx = Context("lenient", threshold_tokens=200)
    ctx.resolve_sub_goal("discovered mid-flight")
    assert [g.description for g in ctx.resolved_sub_goals] == ["discovered mid-flight"]


def test_symbol_facts_are_deduplicated() -> None:
    ctx = Context("dedupe", threshold_tokens=200)
    fact = SymbolFact("x", SymbolKind.DEFINITION, "a.py:1")
    ctx.record_symbol_fact(fact)
    ctx.record_symbol_fact(fact)
    assert list(ctx.symbol_facts) == [fact]

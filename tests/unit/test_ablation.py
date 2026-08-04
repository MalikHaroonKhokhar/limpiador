"""Offline self-tests for the ablation instruments (ARCHITECTURE.md §15, MEMO).

The ablation turns the defended decision — dynamic tool discovery over a static
fifty-tool registry — into numbers. Two of its three pieces cost nothing and are
unit-tested here like any other code:

* the *retrieval probe* — labelled capability queries scored against the real
  keyword ranker — is deterministic and model-free, so we can pin that a
  well-phrased ("direct") query lands its tool and that an oblique paraphrase
  lands it *less* often (the ARCH_DEBT_001 signal the probe exists to localise);
* the *metric aggregation* — reading tokens, turns, and RESEARCH_RETRY count off
  a finished run — is pure and is tested against a hand-built trace.

The third piece, the A/B agent run, drives the real model and is exercised by
``make ablation``, not here — these tests never call a model.
"""

from __future__ import annotations

import pytest

from evals.ablation import (
    DIRECT,
    OBLIQUE,
    RETRIEVAL_QUERIES,
    ArmMetrics,
    ProbeRow,
    RetrievalQuery,
    RetrievalReport,
    probe_retrieval,
    render_ablation,
    render_retrieval,
    summarize_run,
)
from limpiador.agent.loop import RunResult
from limpiador.observability.tracing import RESEARCH_RETRY_TAG, Tracer
from limpiador.schemas import Schema, TokenUsage
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry, build_registry


# ---- retrieval probe: direct queries land, oblique ones land less often -----
def test_direct_queries_are_ranked_well_and_oblique_ones_worse() -> None:
    """The instrument's whole point: keyword ranking is strong when the query
    shares the tool's vocabulary and degrades on a paraphrase. If those two bands
    ever converge, the ranker changed and the ARCH_DEBT_001 story needs re-telling."""
    report = probe_retrieval(build_registry(), RETRIEVAL_QUERIES, k=5)

    # Direct queries share the tool's own words, so keyword overlap should nearly
    # always surface the right tool; allow one label to drift without failing.
    assert report.hit_rate(DIRECT) >= 0.9
    # The oblique band deliberately avoids the tool's words — it MUST do worse,
    # which is exactly the cost semantic ranking (ARCH_DEBT_001) would recover.
    assert report.hit_rate(OBLIQUE) < report.hit_rate(DIRECT)
    # A hit's mean rank stays near the top of the window (not merely "in the top k").
    assert report.mean_rank(DIRECT) is not None
    assert report.mean_rank(DIRECT) <= 1.5


def test_every_labelled_query_names_a_real_tool() -> None:
    """A stale label (a tool that was renamed away) would silently become a
    permanent miss and quietly depress the hit-rate — so pin the labels to the
    live catalogue."""
    catalogue = set(build_registry().tool_names())
    for query in RETRIEVAL_QUERIES:
        assert query.expected in catalogue, f"unknown expected tool {query.expected!r}"
    assert {q.band for q in RETRIEVAL_QUERIES} == {DIRECT, OBLIQUE}


# ---- the probe mechanism, on a tiny synthetic registry (fully deterministic) --
class _Noop(Schema):
    """An empty, valid I/O contract for a one-off dummy tool."""


def _fake_tool(name: str, description: str) -> Tool:
    """Build and instantiate a one-off dummy Tool subclass (mirrors test_registry)."""
    cls = type(
        name.replace(".", "_").title().replace("_", ""),
        (Tool,),
        {
            "name": name,
            "description": description,
            "Input": _Noop,
            "Output": _Noop,
            "run": lambda self, request: _Noop(),
        },
    )
    return cls()


def test_probe_records_rank_for_a_hit_and_none_for_a_miss() -> None:
    registry = ToolRegistry()
    registry.register(_fake_tool("git.commit", "commit the staged changes"))
    registry.register(_fake_tool("fs.read_file", "read a file's contents"))

    queries = (
        RetrievalQuery("commit the staged changes", "git.commit", DIRECT),
        RetrievalQuery("commit the staged changes", "test.run_tests", OBLIQUE),  # absent
    )
    report = probe_retrieval(registry, queries, k=5)
    by_expected = {row.expected: row for row in report.rows}

    assert by_expected["git.commit"].rank == 1
    assert by_expected["git.commit"].hit is True
    assert by_expected["test.run_tests"].rank is None  # never registered → a miss
    assert by_expected["test.run_tests"].hit is False
    assert report.hit_rate() == pytest.approx(0.5)


def test_a_tool_outside_the_top_k_window_is_a_miss() -> None:
    """Ranking in the catalogue but below the window the model is shown still
    counts as a miss — the model only ever sees the top k summaries."""
    registry = ToolRegistry()
    registry.register(_fake_tool("git.commit", "commit staged changes now"))
    registry.register(_fake_tool("git.status", "status of the commit and tree"))
    registry.register(_fake_tool("git.log", "log of every commit"))

    # All three mention "commit"; k=1 shows only the single top-ranked one.
    report = probe_retrieval(registry, (RetrievalQuery("commit", "git.log", DIRECT),), k=1)
    assert report.rows[0].hit is False


# ---- metric aggregation: read tokens / turns / retries off a finished run ----
def _trace_with(model_calls: list[TokenUsage], research_retries: int) -> Tracer:
    tracer = Tracer()
    for usage in model_calls:
        tracer.record_model_call(model="gpt", latency_s=0.0, usage=usage)
    for _ in range(research_retries):
        tracer(RESEARCH_RETRY_TAG, "query='...'")
    return tracer


def test_summarize_run_totals_tokens_turns_and_research_retries() -> None:
    tracer = _trace_with(
        [TokenUsage(prompt_tokens=1000, completion_tokens=40),
         TokenUsage(prompt_tokens=1200, completion_tokens=55)],
        research_retries=2,
    )
    result = RunResult(
        result="done",
        aborted=False,
        turns=7,
        tool_calls=("search_tools", "load_tool", "git_status"),
        trace=tracer,
    )

    metrics = summarize_run("dynamic", result, passed=True)

    assert metrics.label == "dynamic"
    assert metrics.passed is True
    assert metrics.turns == 7
    assert metrics.tool_calls == 3
    assert metrics.prompt_tokens == 2200
    assert metrics.completion_tokens == 95
    assert metrics.total_tokens == 2295
    assert metrics.research_retries == 2
    assert metrics.samples == 1


def test_mean_arm_metrics_averages_runs_and_conjoins_the_outcome() -> None:
    from evals.ablation import mean_arm_metrics

    runs = [
        ArmMetrics("dynamic", passed=True, turns=10, tool_calls=10,
                   prompt_tokens=1000, completion_tokens=100, total_tokens=1100, research_retries=1),
        ArmMetrics("dynamic", passed=False, turns=20, tool_calls=20,
                   prompt_tokens=3000, completion_tokens=200, total_tokens=3200, research_retries=3),
    ]
    mean = mean_arm_metrics("dynamic", runs)

    assert mean.samples == 2
    assert mean.turns == 15  # (10 + 20) / 2
    assert mean.total_tokens == 2150  # (1100 + 3200) / 2
    assert mean.research_retries == 2  # (1 + 3) / 2
    # One sample failed, so the averaged arm is NOT reported as a pass.
    assert mean.passed is False


# ---- report rendering shows both arms and the token delta -------------------
def test_render_ablation_reports_both_arms_and_the_delta() -> None:
    from evals.ablation import AblationRow

    dynamic = ArmMetrics("dynamic", passed=True, turns=6, tool_calls=8,
                         prompt_tokens=5000, completion_tokens=200,
                         total_tokens=5200, research_retries=1)
    static = ArmMetrics("full-menu", passed=True, turns=6, tool_calls=6,
                        prompt_tokens=15000, completion_tokens=180,
                        total_tokens=15180, research_retries=0)
    text = render_ablation([AblationRow("fix_failing_test", dynamic, static)])

    assert "fix_failing_test" in text
    assert "dynamic" in text and "full-menu" in text
    assert "5200" in text and "15180" in text  # both arms' totals surface
    # The full-menu arm spent far more tokens; the delta must be visible, signed.
    assert "%" in text
    # Trustworthiness footnotes: the definitional zero and the single-sample caveat.
    assert "n=1" in text
    assert "by construction" in text
    assert "variance" in text


def test_render_retrieval_shows_both_bands_and_a_hit_rate() -> None:
    report = RetrievalReport([
        ProbeRow("commit the staged changes", "git.commit", DIRECT, rank=1),
        ProbeRow("throw away my edits", "git.reset", OBLIQUE, rank=None),
    ])
    text = render_retrieval(report)
    assert DIRECT in text and OBLIQUE in text
    assert "git.reset" in text  # the miss is spelled out (that is the actionable row)
    assert "%" in text  # a hit-rate is rendered

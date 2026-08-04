"""Ablation: dynamic tool discovery vs. the full-menu baseline (ARCHITECTURE.md §15).

Turns the MEMO's defended decision — dynamic tool loading over a static
fifty-tool registry — into numbers. Three instruments, two of them free:

* a **retrieval probe** (free, deterministic, no model): labelled capability
  queries scored against the real keyword ranker, reporting hit-rate@k and mean
  rank, split into a ``direct`` band (queries that share the tool's vocabulary)
  and an ``oblique`` band (queries that deliberately do not). The gap between the
  bands localises ARCH_DEBT_001 — exactly where a paraphrase costs a turn, and
  exactly what semantic ranking would recover. This half runs anywhere.

* an **A/B agent run** (real model, costs credits): the agent cases driven once
  through the dynamic-discovery :class:`~limpiador.tools.registry.ToolRegistry`
  and once through the full-menu
  :class:`~limpiador.tools.registry.StaticRegistry`, comparing total tokens,
  turns, the ``[REGISTRY RESEARCH_RETRY]`` count, and outcome. Real-only; skips
  cleanly when ``OPENAI_API_KEY`` is unset, exactly like the eval harness — a
  cheaper arm that *fails the task* is not a win, so outcome is reported too.

* **metric aggregation** (free): reading tokens/turns/retries off a finished run.

Run it: ``make ablation`` (or ``python -m evals.ablation``). The retrieval table
always prints; the A/B table prints only when a key is configured.

Two honesty caveats the report itself surfaces, kept here so they are not lost:

* **Sample size.** ``repeats`` defaults to 1, so the default A/B figures are
  *single point estimates* — even at ``temperature=0`` the model is not perfectly
  deterministic. Raise ``repeats`` to report a mean over several samples when a
  headline number needs to survive scrutiny; the rendered ``n=`` states it.
* **The prompt is held constant** across both arms (the correct A/B choice), but
  the shared ``_system_prompt`` is mildly discovery-flavoured ("Discover the
  tools you need"). It is coherent for the static arm — that arm is never shown
  ``search_tools``/``load_tool`` so it cannot call them — but the constant is not
  perfectly neutral, which slightly favours the dynamic framing.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from evals.cases import fix_failing_test, safe_rename
from evals.cases._base import EvalCase
from evals.harness import _eval_adapter, _in_dir, checkout_fixture
from limpiador.agent.guard import CallGuard
from limpiador.agent.llm import LLMAdapter
from limpiador.agent.loop import RunResult, run
from limpiador.cli import _system_prompt
from limpiador.observability.tracing import RESEARCH_RETRY_TAG, Tracer
from limpiador.schemas import SearchToolsRequest
from limpiador.tools.registry import (
    ToolRegistry,
    build_registry,
    build_static_registry,
)

# ============================================================================
# The retrieval probe — free, deterministic, no model (ARCH_DEBT_001 instrument)
# ============================================================================

# The two bands. A ``direct`` query phrases the need in the tool's own vocabulary
# (its name or the synonyms baked into its description); an ``oblique`` query says
# the same thing in words the tool never uses. Keyword overlap thrives on the
# first and degrades on the second — the whole reason semantic ranking is on the
# roadmap — so the probe carries both and reports them apart.
DIRECT = "direct"
OBLIQUE = "oblique"


@dataclass(frozen=True)
class RetrievalQuery:
    """One labelled probe: a capability phrasing and the tool it should surface."""

    query: str
    expected: str
    band: str


# Labels are pinned to the live catalogue by ``test_ablation`` — a renamed tool
# would otherwise become a silent permanent miss. The oblique set is authored to
# avoid each target's own words; several of them genuinely miss today, which is
# the point (the number is the debt).
RETRIEVAL_QUERIES: tuple[RetrievalQuery, ...] = (
    # -- direct: phrased in the tool's own vocabulary -------------------------
    RetrievalQuery("run the test suite and see what fails", "test.run_tests", DIRECT),
    RetrievalQuery(
        "find every place this function is used across the repository", "ast.find_references", DIRECT
    ),
    RetrievalQuery("rename a symbol at every reference site", "ast.rename_symbol", DIRECT),
    RetrievalQuery("read the contents of a file", "fs.read_file", DIRECT),
    RetrievalQuery("search file contents for a pattern", "fs.grep", DIRECT),
    RetrievalQuery("open a pull request from a branch into main", "github.create_pr", DIRECT),
    RetrievalQuery("create a new git branch and switch to it", "git.branch_create", DIRECT),
    RetrievalQuery("commit the staged changes with a message", "git.commit", DIRECT),
    RetrievalQuery("show the working tree status", "git.status", DIRECT),
    RetrievalQuery("where is this symbol defined", "ast.find_definition", DIRECT),
    RetrievalQuery("list the symbols a file defines", "ast.list_symbols", DIRECT),
    RetrievalQuery("apply a unified diff patch to the working tree", "fs.apply_patch", DIRECT),
    RetrievalQuery("run the type checker", "test.typecheck", DIRECT),
    RetrievalQuery("run the linter", "test.lint", DIRECT),
    RetrievalQuery("stage files for the next commit", "git.stage", DIRECT),
    # -- oblique: the same needs, in words the tool never uses ----------------
    RetrievalQuery("undo my last change and go back to how things were", "git.reset", OBLIQUE),
    RetrievalQuery("throw away all my edits and start fresh from the last commit", "git.reset", OBLIQUE),
    RetrievalQuery("who last touched this line of code", "git.blame", OBLIQUE),
    RetrievalQuery("how tangled and hard to follow is this file", "ast.complexity_score", OBLIQUE),
    RetrievalQuery("surface parts of the code nobody uses anymore", "ast.find_dead_code", OBLIQUE),
    RetrievalQuery("temporarily set my work aside so I can switch tasks", "git.stash", OBLIQUE),
    RetrievalQuery("spot circular imports between modules", "ast.detect_cycles", OBLIQUE),
    RetrievalQuery("make sure the types line up before shipping", "test.typecheck", OBLIQUE),
    RetrievalQuery("pull out this block of logic into its own helper", "ast.extract_function", OBLIQUE),
    RetrievalQuery("bundle my changes up and send them for review", "github.create_pr", OBLIQUE),
)


@dataclass(frozen=True)
class ProbeRow:
    """One probe's result: where (if at all) the expected tool ranked, top-k.

    ``rank`` is 1-based within the shown window, or ``None`` when the expected
    tool fell outside the top k the model would see — which counts as a miss.
    """

    query: str
    expected: str
    band: str
    rank: int | None

    @property
    def hit(self) -> bool:
        return self.rank is not None


@dataclass(frozen=True)
class RetrievalReport:
    """The probe rows, with hit-rate and mean-rank views over any band."""

    rows: list[ProbeRow]

    def _select(self, band: str | None) -> list[ProbeRow]:
        return [r for r in self.rows if band is None or r.band == band]

    def hit_rate(self, band: str | None = None) -> float:
        rows = self._select(band)
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.hit) / len(rows)

    def mean_rank(self, band: str | None = None) -> float | None:
        """Mean 1-based rank over the *hits* in a band; ``None`` if none hit."""
        ranks = [r.rank for r in self._select(band) if r.rank is not None]
        if not ranks:
            return None
        return sum(ranks) / len(ranks)


def probe_retrieval(
    registry: ToolRegistry,
    queries: Sequence[RetrievalQuery] = RETRIEVAL_QUERIES,
    k: int = 5,
) -> RetrievalReport:
    """Score each labelled query against the registry's real ranker, top-k.

    Deterministic and model-free: it calls the same ``search_tools`` the agent
    calls and records where the expected tool landed in the window the model
    would actually be shown. Nothing here costs a credit.
    """
    rows: list[ProbeRow] = []
    for q in queries:
        result = registry.search(SearchToolsRequest(query=q.query, limit=k))
        names = [summary.name for summary in result.summaries]
        rank = names.index(q.expected) + 1 if q.expected in names else None
        rows.append(ProbeRow(q.query, q.expected, q.band, rank))
    return RetrievalReport(rows)


# ============================================================================
# Metric aggregation — read tokens / turns / retries off a finished run (free)
# ============================================================================


@dataclass(frozen=True)
class ArmMetrics:
    """One arm's cost and outcome for a case, averaged over ``samples`` runs.

    ``samples`` is 1 for a single run and its true count after
    :func:`mean_arm_metrics`; the numeric fields are means over those runs, and
    ``passed`` is the *conjunction* — ``True`` only if the arm passed every
    sample, so a single flaky failure is not reported as a pass.
    """

    label: str
    passed: bool | None
    turns: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    research_retries: int
    samples: int = 1


def summarize_run(label: str, result: RunResult, *, passed: bool | None) -> ArmMetrics:
    """Reduce a finished :class:`RunResult` to the arm's comparable metrics.

    Tokens and the RESEARCH_RETRY count come off the run's tracer; turns and the
    dispatched-call count come off the result. ``passed`` is supplied by the
    caller because judging the outcome needs the mutated checkout, not just the
    run — but a run with no tracer still yields well-defined zeros.
    """
    tracer = result.trace or Tracer()
    return ArmMetrics(
        label=label,
        passed=passed,
        turns=result.turns,
        tool_calls=len(result.tool_calls),
        prompt_tokens=tracer.total_prompt_tokens(),
        completion_tokens=tracer.total_completion_tokens(),
        total_tokens=tracer.total_tokens(),
        research_retries=tracer.count_tag(RESEARCH_RETRY_TAG),
        samples=1,
    )


def mean_arm_metrics(label: str, runs: Sequence[ArmMetrics]) -> ArmMetrics:
    """Average a set of same-arm runs into one, tracking the sample count.

    Numeric fields become the mean (rounded); ``passed`` is the conjunction over
    the runs (``None`` if any sample's outcome was itself unknown), so a headline
    figure over ``repeats > 1`` is variance-aware rather than a lone sample."""
    n = len(runs)

    def mean(attr: str) -> int:
        return round(sum(getattr(r, attr) for r in runs) / n)

    outcomes = [r.passed for r in runs]
    passed = None if any(o is None for o in outcomes) else all(outcomes)
    return ArmMetrics(
        label=label,
        passed=passed,
        turns=mean("turns"),
        tool_calls=mean("tool_calls"),
        prompt_tokens=mean("prompt_tokens"),
        completion_tokens=mean("completion_tokens"),
        total_tokens=mean("total_tokens"),
        research_retries=mean("research_retries"),
        samples=n,
    )


@dataclass(frozen=True)
class AblationRow:
    """One case scored both ways: dynamic discovery vs. the full-menu baseline."""

    case_name: str
    dynamic: ArmMetrics
    static: ArmMetrics

    def token_delta_pct(self) -> float | None:
        """The full-menu arm's token cost relative to dynamic, as a signed %.

        ``+120%`` means the static baseline spent 2.2x the tokens dynamic did.
        ``None`` when dynamic spent nothing (no real model ran)."""
        if self.dynamic.total_tokens == 0:
            return None
        delta = self.static.total_tokens - self.dynamic.total_tokens
        return 100.0 * delta / self.dynamic.total_tokens


# ============================================================================
# Rendering
# ============================================================================

_RULE = "=" * 68


def render_retrieval(report: RetrievalReport) -> str:
    """The retrieval table: overall and per-band hit-rate and mean rank, then the
    misses spelled out (a miss is where semantic ranking would earn its keep)."""
    lines = [_RULE, "RETRIEVAL PROBE — keyword ranker, top-5 (free, no model)", _RULE]

    def _band_line(band: str | None, title: str) -> str:
        rate = report.hit_rate(band)
        mean = report.mean_rank(band)
        rank_txt = f"{mean:.2f}" if mean is not None else "—"
        n = len(report._select(band))
        hits = sum(1 for r in report._select(band) if r.hit)
        return f"  {title:9} hit-rate {rate:6.0%}  ({hits}/{n})   mean rank {rank_txt}"

    lines.append(_band_line(DIRECT, DIRECT))
    lines.append(_band_line(OBLIQUE, OBLIQUE))
    lines.append(_band_line(None, "overall"))

    misses = [r for r in report.rows if not r.hit]
    if misses:
        lines.append("")
        lines.append(f"  misses ({len(misses)}) — where a paraphrase fell out of the top 5:")
        for row in misses:
            lines.append(f"    ✗ {row.expected:22} <- {row.query!r}")
    return "\n".join(lines)


def render_ablation(rows: Sequence[AblationRow]) -> str:
    """The A/B table: both arms' tokens, turns, retries, and outcome per case,
    plus the signed token delta the whole exercise is about."""
    lines = [_RULE, "ABLATION — dynamic discovery vs. full-menu baseline (real model)", _RULE]
    samples = rows[0].dynamic.samples if rows else 1
    for row in rows:
        delta = row.token_delta_pct()
        delta_txt = f"{delta:+.0f}%" if delta is not None else "n/a"
        lines.append("")
        lines.append(
            f"[{row.case_name}]  n={row.dynamic.samples}   "
            f"full-menu tokens vs dynamic: {delta_txt}"
        )
        lines.append(
            f"  {'arm':10} {'pass':>4} {'turns':>6} {'calls':>6} "
            f"{'prompt':>9} {'compl':>7} {'total':>9} {'retry':>6}"
        )
        for arm in (row.dynamic, row.static):
            passed = "?" if arm.passed is None else ("✓" if arm.passed else "✗")
            lines.append(
                f"  {arm.label:10} {passed:>4} {arm.turns:>6} {arm.tool_calls:>6} "
                f"{arm.prompt_tokens:>9} {arm.completion_tokens:>7} {arm.total_tokens:>9} "
                f"{arm.research_retries:>6}"
            )
    lines.append("")
    lines.append("notes:")
    lines.append("  · full-menu 'retry' is 0 by construction — with no search step, it cannot re-search.")
    if samples == 1:
        lines.append("  · n=1: single point estimates, not variance-controlled. Raise repeats to average.")
    return "\n".join(lines)


# ============================================================================
# The A/B agent run — real model, real fixtures (costs credits)
# ============================================================================

# The agent cases the ablation drives both ways. Both are AGENT-kind and exercise
# the discover→edit→verify chain; the reviewer case runs a different engine
# (spawn_reviewer with its own scoped registry), so it is out of this comparison.
ABLATION_CASES: tuple[EvalCase, ...] = (fix_failing_test.CASE, safe_rename.CASE)


def _run_arm(
    case: EvalCase,
    label: str,
    registry: ToolRegistry,
    adapter: LLMAdapter,
) -> ArmMetrics:
    """Drive one case through one registry on its own fresh checkout, and score it.

    Each arm gets an isolated checkout because an arm mutates files — the two arms
    must not see each other's edits. Outcome is judged with the case's own
    ``check_outcome`` against the mutated checkout, exactly as the eval harness does.
    """
    checkout = checkout_fixture(case.fixture)
    try:
        tracer = Tracer()
        with _in_dir(checkout):
            result = run(
                case.task,
                registry=registry,
                adapter=adapter,
                guard=CallGuard(ceiling=case.max_calls),
                system_prompt=_system_prompt(checkout),
                tracer=tracer,
            )
        passed = not result.aborted and not case.check_outcome(checkout)
        return summarize_run(label, result, passed=passed)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def run_ablation(
    cases: Sequence[EvalCase] = ABLATION_CASES,
    adapter: LLMAdapter | None = None,
    repeats: int = 1,
) -> list[AblationRow]:
    """Score every case both ways — dynamic discovery and the full-menu baseline.

    Real-only: it drives the same adapter the eval harness uses. Each arm is run
    ``repeats`` times, each run on its own fresh registry and isolated checkout,
    and averaged into one :class:`ArmMetrics`. ``repeats=1`` (the default) yields
    a single point estimate — raise it to trade credits for variance control on a
    number that has to hold up.
    """
    adapter = adapter or _eval_adapter()
    rows: list[AblationRow] = []
    for case in cases:
        dynamic = mean_arm_metrics(
            "dynamic",
            [_run_arm(case, "dynamic", build_registry(), adapter) for _ in range(repeats)],
        )
        static = mean_arm_metrics(
            "full-menu",
            [_run_arm(case, "full-menu", build_static_registry(), adapter) for _ in range(repeats)],
        )
        rows.append(AblationRow(case.name, dynamic, static))
    return rows


def main(argv: list[str] | None = None) -> int:
    """Print the retrieval table always; add the A/B table when a key is present."""
    print(render_retrieval(probe_retrieval(build_registry())))
    print()
    if not os.environ.get("OPENAI_API_KEY"):
        print("⏭️  A/B agent run skipped: OPENAI_API_KEY not set (it drives the real model).")
        return 0
    print(render_ablation(run_ablation()))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())

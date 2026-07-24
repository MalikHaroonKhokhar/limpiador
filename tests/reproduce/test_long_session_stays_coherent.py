"""Property #3, end to end, against the real model (HAR-33).

The mock counterpart (``tests/integration/test_long_session_coherence.py``) proves
the wiring deterministically. This is the *real* session the ticket asks for: the
production agent, the full registry, the CLI's own system prompt, on a seeded
multi-module package big enough that the work genuinely spans twenty-plus tool
calls and crosses the compaction threshold more than once.

The assertion is the ticket's acceptance, and it guards the ≥20-call coherent
path:

* ``call_count >= 20`` — the session really is long-horizon;
* the goal was achieved — the rename landed at every call site and the run
  finished rather than dying on the ceiling;
* compaction fired and is visible in the trace;
* the plan stayed coherent — a plan was committed to durable memory, and no
  resolved sub-goal was ever re-litigated.

A captured run of this session lives in ``traces/har-33/``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from limpiador.agent.guard import CallGuard
from limpiador.agent.llm import DEFAULT_ROUTING, OPENAI_MODEL_ENV, OpenAIAdapter
from limpiador.agent.loop import run
from limpiador.cli import _system_prompt
from limpiador.observability.tracing import COMPACTION_TAG
from limpiador.tools.registry import PLAN_RESOLVE, build_registry

pytestmark = pytest.mark.reproduce

_OLD = "calculate_total"
_NEW = "compute_order_total"

_CALL_SITES = ("checkout", "invoice", "report", "refund")


def _filler(module: str, count: int = 4) -> str:
    """Realistic bulk: a module is not five lines, and property #3 only means
    anything when a read is big enough to be worth evicting."""
    blocks = []
    for i in range(count):
        blocks.append(
            f'\ndef {module}_helper_{i}(value, factor=1.0):\n'
            f'    """Adjust ``value`` for the {module} step {i}.\n\n'
            f"    Part of the {module} pipeline. Kept deliberately explicit so the\n"
            f"    module reads like production code rather than a stub, and so a\n"
            f"    read of this file is a payload worth compacting away later.\n"
            f'    """\n'
            f"    adjusted = value * factor\n"
            f"    if adjusted < 0:\n"
            f"        raise ValueError(f'{module} step {i} went negative: {{adjusted}}')\n"
            f"    return round(adjusted, 2)\n"
        )
    return "".join(blocks)


def _call_site_module(name: str, body: str) -> str:
    return (
        f'"""The {name} step of the billing pipeline."""\n\n'
        f"from billing.core import {_OLD}\n\n"
        f"{body}\n"
        f"{_filler(name)}"
    )


# A package wide enough that the symbol must be chased across many files — the
# shape that makes a session long-horizon rather than a two-step edit.
_MODULES: dict[str, str] = {
    "billing/__init__.py": "",
    "billing/core.py": (
        '"""Core totals for the billing package."""\n\n'
        "TAX_RATE = 0.2\n\n\n"
        f"def {_OLD}(items):\n"
        '    """Sum the line items and apply tax."""\n'
        "    subtotal = sum(item['price'] * item['qty'] for item in items)\n"
        "    return round(subtotal * (1 + TAX_RATE), 2)\n"
        f"{_filler('core', 6)}"
    ),
    "billing/checkout.py": _call_site_module(
        "checkout",
        "def checkout(items):\n"
        f"    return {{'due': {_OLD}(items)}}\n",
    ),
    "billing/invoice.py": _call_site_module(
        "invoice",
        "def render_invoice(items):\n"
        f"    return f'Amount due: {{{_OLD}(items)}}'\n",
    ),
    "billing/report.py": _call_site_module(
        "report",
        "def summary(orders):\n"
        f"    return {{'orders': len(orders), 'gross': sum({_OLD}(o) for o in orders)}}\n",
    ),
    "billing/refund.py": _call_site_module(
        "refund",
        "def refund(items):\n"
        f"    return -{_OLD}(items)\n",
    ),
    "tests/test_billing.py": (
        "from billing.checkout import checkout\n"
        "from billing.invoice import render_invoice\n"
        "from billing.refund import refund\n"
        "from billing.report import summary\n\n"
        "ITEMS = [{'price': 10.0, 'qty': 2}, {'price': 5.0, 'qty': 1}]\n\n\n"
        "def test_checkout():\n"
        "    assert checkout(ITEMS) == {'due': 30.0}\n\n\n"
        "def test_invoice():\n"
        "    assert render_invoice(ITEMS) == 'Amount due: 30.0'\n\n\n"
        "def test_refund():\n"
        "    assert refund(ITEMS) == -30.0\n\n\n"
        "def test_summary():\n"
        "    assert summary([ITEMS])['orders'] == 1\n"
    ),
}

_TASK = (
    f"Rename the function `{_OLD}` to `{_NEW}` throughout the billing package in "
    "this local working tree. The code is on disk here — use the filesystem and "
    "AST tools, not remote code search.\n"
    "Work through these five files one at a time, reading each before you edit "
    "it: billing/core.py (the definition), then billing/checkout.py, "
    "billing/invoice.py, billing/report.py and billing/refund.py (each has an "
    "import and a call site). Every occurrence of the old name must be gone.\n"
    "When all five are done, run the test suite and confirm it passes, then "
    "commit the change."
)

# Low enough that a handful of real file reads crosses it, so a genuinely long
# session compacts more than once. The threshold is configuration (§7).
_THRESHOLD = 800
_CEILING = 75

# Set to write the session's trace to traces/har-33/ as the captured evidence the
# ticket asks for. Off by default so an ordinary reproduce run never dirties the
# working tree.
_CAPTURE_ENV = "LIMPIADOR_CAPTURE_TRACE"
_CAPTURE_PATH = Path(__file__).resolve().parents[2] / "traces" / "har-33" / "long-session.md"


def _capture(result, sources: dict[str, str]) -> None:
    """Write the session's trace as durable, readable evidence."""
    trace = result.trace
    lines = [
        "# HAR-33 — a captured 20+ call session that kept its plan",
        "",
        f"Captured {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} "
        "by `tests/reproduce/test_long_session_stays_coherent.py` (real model).",
        "",
        "## Outcome",
        "",
        f"- tool calls: **{len(result.tool_calls)}**",
        f"- model turns: {result.turns}",
        f"- aborted: {result.aborted}",
        f"- compactions recorded: **{trace.count_tag(COMPACTION_TAG)}**",
        f"- sub-goals: {len(result.context.sub_goals)} "
        f"({len(result.context.resolved_sub_goals)} resolved)",
        "",
        "## The plan (durable state — never evicted)",
        "",
    ]
    for goal in result.context.sub_goals:
        lines.append(f"- [{'x' if goal.resolved else ' '}] {goal.description}")
    lines += ["", "## Compaction (property #3, visible in the trace)", ""]
    for tag, message in trace.tags:
        if tag == COMPACTION_TAG:
            lines.append(f"- `{tag}` {message}")
    lines += ["", "## Tool calls, in order", ""]
    for i, entry in enumerate(trace.tool_calls, 1):
        suffix = f"  ← {entry.error}" if entry.error else ""
        lines.append(f"{i:3}. `{entry.name}`{suffix}")
    lines += ["", "## Final state of the package", ""]
    for name, text in sorted(sources.items()):
        lines.append(f"- `{name}`: old name present = {_OLD in text}")
    lines += ["", "## Result", "", "```", str(result.result or "")[:800], "```", ""]

    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CAPTURE_PATH.write_text("\n".join(lines))


def test_a_long_session_keeps_its_plan_and_reaches_the_goal(make_git_repo) -> None:
    repo = make_git_repo(_MODULES)
    root = Path(repo.working_tree_dir)

    # Pinned to the strong tier at temperature 0, like the eval harness: this is a
    # pass/fail gate, so it must not hinge on sampling. (The default routing sends
    # mechanical turns to the cheap tier, which cannot hold a 30-call refactor.)
    adapter = OpenAIAdapter(
        model=os.environ.get(OPENAI_MODEL_ENV) or DEFAULT_ROUTING.strong.name,
        temperature=0.0,
    )

    result = run(
        _TASK,
        registry=build_registry(),
        adapter=adapter,
        guard=CallGuard(ceiling=_CEILING),
        system_prompt=_system_prompt(root),
        threshold_tokens=_THRESHOLD,
    )

    sources = {
        name: (root / name).read_text()
        for name in _MODULES
        if name.endswith(".py") and name != "billing/__init__.py"
    }
    # Capture first, so a failing session is just as inspectable as a passing one.
    if os.environ.get(_CAPTURE_ENV):
        _capture(result, sources)

    calls = list(result.tool_calls)
    # ---- the session is long-horizon ----------------------------------------
    assert len(calls) >= 20, f"expected a 20+ call session; got {len(calls)}: {calls}"
    assert result.aborted is False, f"the run hit the {_CEILING}-call ceiling: {calls}"

    # ---- the goal was actually achieved -------------------------------------
    stale = [name for name, text in sources.items() if _OLD in text]
    assert not stale, f"the old name survived in {stale}"
    renamed = [name for name, text in sources.items() if _NEW in text]
    assert len(renamed) >= 5, f"the new name only reached {renamed}"

    # ---- compaction fired, and the trace says so ----------------------------
    assert result.trace is not None
    assert result.trace.count_tag(COMPACTION_TAG) >= 1, (
        "a 20+ call session over the threshold must compact, and record it"
    )

    # ---- the plan survived and stayed coherent ------------------------------
    context = result.context
    assert context is not None
    assert context.sub_goals, "the agent never committed to a plan"
    assert context.resolved_sub_goals, "the agent never resolved a single sub-goal"

    resolved: list[str] = []
    for call in result.protocol_calls:
        if call.name != PLAN_RESOLVE:
            continue
        goal = str(call.arguments.get("sub_goal", ""))
        assert goal not in resolved, f"sub-goal re-litigated after resolution: {goal!r}"
        resolved.append(goal)

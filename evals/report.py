"""Eval report — a pass/fail summary plus a per-case trace dump (ARCHITECTURE.md §11.3).

An eval result is two-layered, so the report is too: for each case it shows the
*outcome* verdict (did the goal get achieved) and the *trace* verdict (did the
agent reason in the right order, under the ceiling), and dumps the tool-call
sequence the agent actually took — which is what you read when a case fails to
see *how* the reasoning went wrong, not just that it did.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle — report only reads EvalResult's fields
    from evals.harness import EvalResult

_RULE = "=" * 64


def render(results: "Sequence[EvalResult]") -> str:
    """Render eval results into a human-readable report string."""
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    lines = [_RULE, f"EVAL REPORT — {passed}/{total} case(s) passed", _RULE]
    for result in results:
        lines.extend(_render_case(result))
    lines.append("")
    lines.append(_RULE)
    verdict = "ALL PASSED" if passed == total else f"{total - passed} FAILED"
    lines.append(f"RESULT: {verdict}")
    return "\n".join(lines)


def _render_case(result: "EvalResult") -> list[str]:
    """One case's block: its badge, the tool-call trace, and any failures."""
    badge = "PASS" if result.passed else "FAIL"
    sequence = " → ".join(result.tool_calls) if result.tool_calls else "(no tool calls)"
    lines = ["", f"[{badge}] {result.name}", f"  trace ({len(result.tool_calls)} calls): {sequence}"]
    for failure in result.outcome_failures:
        lines.append(f"  outcome ✗ {failure}")
    for failure in result.trace_failures:
        lines.append(f"  trace   ✗ {failure}")
    if result.passed:
        lines.append("  ✓ goal achieved and trace assertions held")
    return lines

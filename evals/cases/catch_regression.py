"""Case: catch a regression in review (fixture ``bad_pr``).

The PR in ``pr.diff`` regresses ``apply_restock`` in ``inventory.py`` — it
subtracts the restock amount instead of adding it. This is the *reviewer* case
(``kind = REVIEWER``): the harness drives the reviewer subagent over the diff
rather than the main agent, because ``spawn_reviewer`` is the parent's
orchestration primitive, not a registry tool the loop can call.

* **outcome** — the reviewer's :class:`ReviewResult` requests changes *and* names
  the real regression in ``inventory.py`` (an approval, or a request for the wrong
  reason, fails the case — it must catch *this* bug, not merely object).
* **trace** — the reviewer stayed read-only: every tool it called is inside its
  scoped allow-list, so it judged the change without ever mutating the repo.
"""

from __future__ import annotations

from evals.cases._base import REVIEWER, EvalCase, reviewer_read_only_names
from limpiador.observability.tracing import Tracer
from limpiador.schemas import ReviewResult, Severity, Verdict

_TASK = (
    "Review the proposed change in this pull request for correctness. The complete "
    "diff and the changed files are provided below and in the working tree — you do "
    "not need to fetch the PR from GitHub. If the change introduces a correctness "
    "regression, return the verdict request_changes and record an error-severity "
    "finding that names the file and line of the defect."
)


def _outcome(review: ReviewResult) -> list[str]:
    failures: list[str] = []
    if review.verdict is not Verdict.REQUEST_CHANGES:
        failures.append(
            f"the reviewer returned {review.verdict.value}, not request_changes — "
            "it did not block the regression"
        )
    flagged = [
        finding
        for finding in review.findings
        if "inventory.py" in finding.file and finding.severity is Severity.ERROR
    ]
    if not flagged:
        failures.append(
            "no error-level finding names inventory.py — the planted regression "
            "was not the reason changes were requested"
        )
    return failures


def _trace(tracer: Tracer) -> list[str]:
    allowed = reviewer_read_only_names()
    used = {entry.name for entry in tracer.tool_calls}
    escaped = sorted(used - allowed)
    if escaped:
        return [f"the reviewer called non-read-only tools: {', '.join(escaped)}"]
    return []


CASE = EvalCase(
    name="catch_regression",
    fixture="bad_pr",
    task=_TASK,
    check_outcome=_outcome,
    check_trace=_trace,
    max_calls=30,
    kind=REVIEWER,
    diff_file="pr.diff",
    changed_files=("inventory.py",),
)

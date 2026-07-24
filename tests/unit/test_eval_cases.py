"""Offline self-tests for the four eval cases (HAR-32).

Each case binds a committed fixture to a task and to two binary assertions —
``check_outcome`` (did the goal get achieved) and ``check_trace`` (did the agent
reason well). The *real-model* scoring runs under ``make eval``; here we prove the
assertions themselves are correct, deterministically and without a model: a fixed
"good" state passes, a fixed "wrong" state fails.

The headline is ``red_herring``: the acceptance for that case is that a *naive
recency heuristic* — editing the most-recently-touched file — genuinely fails it.
``test_red_herring_rejects_editing_the_recent_innocent_file`` is that deliberately
wrong variant, and it must be flagged.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from evals.cases import CASES
from evals.harness import checkout_fixture
from limpiador.observability.tracing import Tracer
from limpiador.schemas import Finding, ReviewResult, Severity, Verdict


def _case(name: str):
    return next(case for case in CASES if case.name == name)


@contextmanager
def _checkout(fixture: str) -> Iterator[Path]:
    path = checkout_fixture(fixture)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _tracer(*tools: str) -> Tracer:
    tracer = Tracer()
    for tool in tools:
        tracer.record_tool_call(tool=tool, latency_s=0.0, input={}, output="")
    return tracer


# ---- the suite is exactly the four ticketed cases ---------------------------
def test_the_four_cases_are_registered_against_their_fixtures() -> None:
    by_name = {case.name: case for case in CASES}
    assert set(by_name) == {"fix_failing_test", "safe_rename", "catch_regression", "red_herring"}
    assert by_name["fix_failing_test"].fixture == "failing_test"
    assert by_name["safe_rename"].fixture == "rename_symbol"
    assert by_name["catch_regression"].fixture == "bad_pr"
    assert by_name["red_herring"].fixture == "red_herring"


# ---- fix_failing_test -------------------------------------------------------
def test_fix_failing_test_outcome_tracks_the_suite() -> None:
    case = _case("fix_failing_test")
    with _checkout("failing_test") as checkout:
        assert case.check_outcome(checkout), "the seeded bug must fail the outcome"
        calc = checkout / "calc.py"
        calc.write_text(calc.read_text().replace("return a - b", "return a + b"))
        assert case.check_outcome(checkout) == []


def test_fix_failing_test_trace_requires_running_the_tests() -> None:
    check = _case("fix_failing_test").check_trace
    assert check(_tracer("fs_read_file", "fs_apply_patch", "test_run_tests")) == []
    assert check(_tracer("fs_read_file", "fs_apply_patch")), "fixing blind must be flagged"


# ---- safe_rename ------------------------------------------------------------
def test_safe_rename_outcome_requires_all_three_sites_and_green_tests() -> None:
    case = _case("safe_rename")
    with _checkout("rename_symbol") as checkout:
        assert case.check_outcome(checkout), "the un-renamed fixture must fail"
        for rel in ("pkg/core.py", "pkg/consumer.py", "pkg/report.py"):
            path = checkout / rel
            path.write_text(path.read_text().replace("compute", "calculate"))
        assert case.check_outcome(checkout) == []


def test_safe_rename_outcome_flags_a_missed_third_site() -> None:
    case = _case("safe_rename")
    with _checkout("rename_symbol") as checkout:
        # Rename only two of the three known sites — report.py is left behind.
        for rel in ("pkg/core.py", "pkg/consumer.py"):
            path = checkout / rel
            path.write_text(path.read_text().replace("compute", "calculate"))
        failures = case.check_outcome(checkout)
        assert any("report.py" in failure for failure in failures)


def test_safe_rename_trace_requires_find_references_before_rename() -> None:
    check = _case("safe_rename").check_trace
    assert check(_tracer("ast_find_references", "ast_rename_symbol")) == []
    assert check(_tracer("ast_rename_symbol")), "renaming without resolving refs is flagged"
    assert check(_tracer("ast_rename_symbol", "ast_find_references")), "resolving after is flagged"


# ---- catch_regression (the reviewer case) -----------------------------------
def test_catch_regression_is_a_reviewer_case_over_the_pr_diff() -> None:
    case = _case("catch_regression")
    assert case.kind == "reviewer"
    assert case.diff_file == "pr.diff"
    assert "inventory.py" in case.changed_files


def test_catch_regression_outcome_passes_when_the_regression_is_flagged() -> None:
    check = _case("catch_regression").check_outcome
    flagged = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        findings=[
            Finding(
                severity=Severity.ERROR,
                file="inventory.py",
                line=6,
                message="apply_restock subtracts the amount instead of adding it",
            )
        ],
    )
    assert check(flagged) == []


def test_catch_regression_outcome_fails_an_approval_or_a_blind_request() -> None:
    check = _case("catch_regression").check_outcome
    approved = ReviewResult(verdict=Verdict.APPROVE, findings=[])
    assert check(approved), "approving a regressing PR must fail the case"
    off_target = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        findings=[Finding(severity=Severity.INFO, file="README.md", message="a typo")],
    )
    assert check(off_target), "requesting changes for the wrong reason must fail"


def test_catch_regression_trace_demands_the_reviewer_stayed_read_only() -> None:
    check = _case("catch_regression").check_trace
    assert check(_tracer("fs_read_file", "ast_find_references", "finish")) == []
    assert check(_tracer("fs_read_file", "fs_apply_patch")), "a write tool must be flagged"


# ---- red_herring: a naive recency heuristic must genuinely fail --------------
def test_red_herring_rejects_editing_the_recent_innocent_file() -> None:
    # The deliberately wrong variant: a recency-driven agent edits the
    # most-recently-touched file (settings.py) and never fixes the real cause.
    case = _case("red_herring")
    with _checkout("red_herring") as checkout:
        settings = checkout / "settings.py"
        settings.write_text(settings.read_text() + "\n# touched by a recency guess\n")
        failures = case.check_outcome(checkout)
        assert failures, "editing the recent red herring must NOT satisfy the case"
        assert any("settings.py" in failure for failure in failures)


def test_red_herring_accepts_fixing_the_true_cause() -> None:
    case = _case("red_herring")
    with _checkout("red_herring") as checkout:
        pipeline = checkout / "pipeline.py"
        pipeline.write_text(pipeline.read_text().replace("rows[1:]", "rows"))
        assert case.check_outcome(checkout) == []


def test_red_herring_trace_requires_gathering_evidence() -> None:
    check = _case("red_herring").check_trace
    assert check(_tracer("test_run_tests", "fs_read_file", "fs_apply_patch", "test_run_tests")) == []
    assert check(_tracer("fs_apply_patch")), "fixing blind without reproducing is flagged"

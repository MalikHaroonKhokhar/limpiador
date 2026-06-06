"""Offline self-tests for the eval harness machinery (HAR-30).

The eval *runner* itself is code, so its non-model parts are unit-tested like any
other code: fixture checkout is isolated and leaves the committed original
pristine, the outcome checks distinguish a fixed repo from a broken one, the
trace checks read the Tracer correctly, and the report renders both verdicts.
The real-model scoring (``evaluate_case``) is exercised by ``make eval``, not
here — these tests cost nothing and never call a model.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from limpiador.observability.tracing import Tracer

from evals.cases import CASES
from evals.harness import EvalResult, checkout_fixture
from evals.report import render

_FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


def _case(name: str):
    return next(case for case in CASES if case.name == name)


# ---- isolation: a checkout is fresh and the committed source stays pristine ---
def test_checkout_is_isolated_and_leaves_the_source_pristine() -> None:
    source_calc = (_FIXTURES / "failing_test" / "calc.py").read_text()
    checkout = checkout_fixture("failing_test")
    try:
        # a real, materialised git repo with the fixture's files...
        assert (checkout / ".git").is_dir()
        assert (checkout / "calc.py").exists() and (checkout / "test_calc.py").exists()

        # ...and mutating the checkout does not touch the committed original.
        (checkout / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (checkout / "GARBAGE.txt").write_text("scribble")
        assert (_FIXTURES / "failing_test" / "calc.py").read_text() == source_calc
        assert not (_FIXTURES / "failing_test" / "GARBAGE.txt").exists()
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


# ---- outcome checks distinguish a broken repo from a fixed one ---------------
def test_outcome_check_fails_on_the_bug_and_passes_once_fixed() -> None:
    check = _case("fix_failing_test").check_outcome
    checkout = checkout_fixture("failing_test")
    try:
        assert check(checkout), "the seeded bug must fail the outcome check"
        (checkout / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
        )
        assert check(checkout) == [], "the fixed repo must pass the outcome check"
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def test_outcome_check_flags_a_disturbed_red_herring() -> None:
    check = _case("fix_failing_test").check_outcome
    checkout = checkout_fixture("failing_test")
    try:
        # Fix the real bug but also disturb the innocent file: still a failure.
        (checkout / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
        )
        (checkout / "formatter.py").write_text("def pretty(value):\n    return str(value)\n")
        failures = check(checkout)
        assert any("formatter.py" in f for f in failures)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def test_rename_outcome_check_detects_an_incomplete_rename() -> None:
    check = _case("rename_symbol_across_files").check_outcome
    checkout = checkout_fixture("rename_symbol")
    try:
        assert check(checkout), "the un-renamed fixture must fail the outcome check"
        # A complete rename across both files passes.
        for rel in ("pkg/core.py", "pkg/consumer.py"):
            path = checkout / rel
            path.write_text(path.read_text().replace("compute", "calculate"))
        assert check(checkout) == []
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


# ---- trace checks read the Tracer (the right tools, the right order) ---------
def _tracer_for(*tools: str) -> Tracer:
    tracer = Tracer()
    for tool in tools:
        tracer.record_tool_call(tool=tool, latency_s=0.0, input={}, output="")
    return tracer


def test_rename_trace_requires_find_references_before_rename() -> None:
    check = _case("rename_symbol_across_files").check_trace
    assert check(_tracer_for("ast_find_references", "ast_rename_symbol")) == []
    # rename without resolving first → flagged
    assert check(_tracer_for("ast_rename_symbol"))
    # resolved AFTER the rename → flagged
    assert check(_tracer_for("ast_rename_symbol", "ast_find_references"))


def test_fix_trace_requires_running_the_tests() -> None:
    check = _case("fix_failing_test").check_trace
    assert check(_tracer_for("fs_read_file", "fs_apply_patch", "test_run_tests")) == []
    assert check(_tracer_for("fs_read_file", "fs_apply_patch"))


# ---- report renders both verdicts and the trace -----------------------------
def test_report_renders_pass_fail_and_the_trace() -> None:
    results = [
        EvalResult("good_case", True, [], [], ("fs_read_file", "test_run_tests")),
        EvalResult("bad_case", False, ["tests still fail"], ["renamed too early"], ("ast_rename_symbol",)),
    ]
    report = render(results)

    assert "1/2 case(s) passed" in report
    assert "[PASS] good_case" in report
    assert "[FAIL] bad_case" in report
    assert "fs_read_file → test_run_tests" in report
    assert "tests still fail" in report
    assert "renamed too early" in report
    assert "RESULT: 1 FAILED" in report

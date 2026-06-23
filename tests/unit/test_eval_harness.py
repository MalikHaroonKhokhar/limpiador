"""Offline self-tests for the eval harness machinery (HAR-30).

The eval *runner* itself is code, so its non-model parts are unit-tested like any
other code: fixture checkout is isolated and leaves the committed original
pristine, and the report renders both verdicts. The two-layer *case* assertions
are covered in ``test_eval_cases.py``; the real-model scoring (``evaluate_case``)
is exercised by ``make eval``, not here — these tests cost nothing and never call
a model.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from evals.harness import EvalResult, checkout_fixture
from evals.report import render

_FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


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

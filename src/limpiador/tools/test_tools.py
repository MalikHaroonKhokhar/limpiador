"""``test.*`` / ``ci.*`` namespace — verification (ARCHITECTURE.md §5.3, 8 tools).

run_tests, run_subset, coverage, lint, typecheck, format, trigger_ci,
get_ci_status. ``run_tests`` emits a typed ``TestResult`` with structured
failures (``{test, file, line, message}``) that the agent consumes to locate and
fix the cause, then re-runs — the verification half of the fix loop (§8).
"""

from __future__ import annotations

from limpiador.schemas import (
    CiStatusRequest,
    CiStatusResult,
    CiTriggerRequest,
    CiTriggerResult,
    CoverageResult,
    FormatResult,
    LintResult,
    TestCoverageRequest,
    TestFormatRequest,
    TestLintRequest,
    TestResult,
    TestRunRequest,
    TestRunSubsetRequest,
    TestTypecheckRequest,
    TypecheckResult,
)
from limpiador.tools.base import declared_tool

TOOLS = (
    declared_tool("test.run_tests", "Run the test suite and report structured pass/fail results.", TestRunRequest, TestResult),
    declared_tool("test.run_subset", "Run a targeted subset of tests by node id or pattern.", TestRunSubsetRequest, TestResult),
    declared_tool("test.coverage", "Run the suite under coverage and report line coverage.", TestCoverageRequest, CoverageResult),
    declared_tool("test.lint", "Run the linter and report violations.", TestLintRequest, LintResult),
    declared_tool("test.typecheck", "Run the type checker and report type errors.", TestTypecheckRequest, TypecheckResult),
    declared_tool("test.format", "Run the formatter and report which files changed.", TestFormatRequest, FormatResult),
    declared_tool("ci.trigger_ci", "Trigger a CI workflow run for a ref.", CiTriggerRequest, CiTriggerResult),
    declared_tool("ci.get_ci_status", "Fetch the status and conclusion of a CI run.", CiStatusRequest, CiStatusResult),
)

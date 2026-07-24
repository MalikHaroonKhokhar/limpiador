"""``test.*`` / ``ci.*`` namespace — verification (ARCHITECTURE.md §5.3, 8 tools).

run_tests, run_subset, coverage, lint, typecheck, format, trigger_ci,
get_ci_status. This is the verification half of the fix loop (§8): ``run_tests``
does not merely report that something failed, it emits a typed :class:`TestResult`
whose failures are structured ``{test, file, line, message}`` objects — the
exact shape the agent reads to locate a cause, edit, and re-run. That structured
handoff is Property #5 at the verification boundary.

The local tools shell out to the real toolchain (pytest, coverage, ruff, mypy);
when a tool is absent from the environment the failure is a *typed*
``ToolUnavailableError`` the loop can fold and adapt to, never a raw traceback.
The two ``ci.*`` tools compose the github namespace — they reuse the same
injectable :class:`GitHubSession` and resilient boundary the ``github.*`` tools
do, so a CI dispatch is metered and retried exactly like any other API call.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from limpiador.observability.errors import ToolUnavailableError
from limpiador.schemas import (
    CiStatusRequest,
    CiStatusResult,
    CiTriggerRequest,
    CiTriggerResult,
    CoverageResult,
    FileCoverage,
    FormatResult,
    LintIssue,
    LintResult,
    TestCoverageRequest,
    TestFailure,
    TestFormatRequest,
    TestLintRequest,
    TestResult,
    TestRunRequest,
    TestRunSubsetRequest,
    TestTypecheckRequest,
    TypeCheckError,
    TypecheckResult,
)
from limpiador.tools.base import Tool
from limpiador.tools.github_tools import GitHubSession, _GitHubTool

_SUBPROCESS_TIMEOUT_S = 300


# ---- running an external toolchain, with a typed error when it is absent ----
def _root() -> Path:
    return Path.cwd()


def _require(module: str) -> None:
    """Raise a typed error if the toolchain ``module`` is not installed."""
    if importlib.util.find_spec(module) is None:
        raise ToolUnavailableError(
            f"the '{module}' toolchain is not installed in this environment; "
            "the agent can skip this step or the operator can install it."
        )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a toolchain command from the repo root, capturing its output."""
    try:
        return subprocess.run(
            command,
            cwd=_root(),
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except FileNotFoundError as error:
        raise ToolUnavailableError(f"{command[0]!r} is not available: {error}") from error


# ---- pytest: structured failures the fix loop consumes ----------------------
def _case_id(case: ElementTree.Element) -> str:
    classname, name = case.get("classname", ""), case.get("name", "test")
    return f"{classname}::{name}" if classname else name


def _case_line(case: ElementTree.Element) -> int | None:
    raw = case.get("line")
    return int(raw) + 1 if raw is not None and raw.isdigit() else None


def _failure_of(case: ElementTree.Element) -> ElementTree.Element | None:
    problem = case.find("failure")
    return problem if problem is not None else case.find("error")


# The ``<file>:<line>:`` frames pytest writes into a failure's traceback text —
# the JUnit ``file``/``line`` attributes are absent under the default xunit2
# family, so the real cause location is recovered from the last such frame.
_TRACE_FRAME = re.compile(r"^(.+?\.py):(\d+):", re.MULTILINE)


def _location(case: ElementTree.Element, problem: ElementTree.Element) -> tuple[str, int | None]:
    """The failing file and line — from the case attributes, else the traceback."""
    file, line = case.get("file"), _case_line(case)
    frames = _TRACE_FRAME.findall(problem.text or "")
    if frames:
        file = file or frames[-1][0]
        line = line or int(frames[-1][1])
    return file or "unknown", line


def _to_failure(case: ElementTree.Element, problem: ElementTree.Element) -> TestFailure:
    message = (problem.get("message") or (problem.text or "test failed")).strip()
    file, line = _location(case, problem)
    return TestFailure(
        test=_case_id(case),
        file=file,
        line=line,
        message=message.splitlines()[0] if message else "test failed",
    )


def _parse_junit(report: Path, proc: subprocess.CompletedProcess[str]) -> TestResult:
    """Project a pytest JUnit-XML report onto the typed :class:`TestResult`."""
    if not report.exists():
        raise ToolUnavailableError(
            f"pytest produced no report (exit {proc.returncode}): {proc.stdout[-400:]}"
        )
    passed = 0
    failures: list[TestFailure] = []
    for case in ElementTree.parse(report).iter("testcase"):
        problem = _failure_of(case)
        if problem is None:
            passed += 1
            continue
        failures.append(_to_failure(case, problem))
    return TestResult(passed=passed, failed=len(failures), failures=failures)


def _run_pytest(targets: list[str], extra: list[str]) -> TestResult:
    """Run pytest over targets and return the structured result."""
    _require("pytest")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        command = [
            sys.executable, "-m", "pytest", *targets,
            "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *extra,
        ]
        return _parse_junit(report, _run(command))


class TestRunTests(Tool):
    name = "test.run_tests"
    description = (
        "Run the test suite and report structured pass/fail results — each failure "
        "as {test, file, line, message}. Synonyms: pytest, run tests, verify, did "
        "it break, check the build."
    )
    Input = TestRunRequest
    Output = TestResult

    def run(self, request: TestRunRequest) -> TestResult:
        extra = ["-m", request.markers] if request.markers else []
        return _run_pytest([request.path], extra)


class TestRunSubset(Tool):
    name = "test.run_subset"
    description = (
        "Run a targeted subset of tests by node id or pattern. Synonyms: run one "
        "test, focused run, just these tests, rerun failures, targeted verify."
    )
    Input = TestRunSubsetRequest
    Output = TestResult

    def run(self, request: TestRunSubsetRequest) -> TestResult:
        return _run_pytest(list(request.tests), [])


# ---- coverage ---------------------------------------------------------------
def _coverage_files(data: dict[str, object]) -> list[FileCoverage]:
    files = data.get("files", {})
    rows: list[FileCoverage] = []
    for name, info in files.items():
        percent = info.get("summary", {}).get("percent_covered", 0.0)
        rows.append(FileCoverage(file=name, percent=round(float(percent), 2)))
    return rows


class TestCoverage(Tool):
    name = "test.coverage"
    description = (
        "Run the suite under coverage and report total and per-file line coverage. "
        "Synonyms: coverage, how much is tested, untested lines, percent covered."
    )
    Input = TestCoverageRequest
    Output = CoverageResult

    def run(self, request: TestCoverageRequest) -> CoverageResult:
        _require("coverage")
        _run([sys.executable, "-m", "coverage", "run", "-m", "pytest", request.path, "-q",
              "-p", "no:cacheprovider"])
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "coverage.json"
            _run([sys.executable, "-m", "coverage", "json", "-o", str(report)])
            if not report.exists():
                raise ToolUnavailableError("coverage produced no report")
            data = json.loads(report.read_text())
        total = float(data.get("totals", {}).get("percent_covered", 0.0))
        return CoverageResult(total_percent=round(total, 2), files=_coverage_files(data))


# ---- lint / typecheck / format (real toolchains; typed if absent) -----------
def _lint_issue(raw: dict[str, object]) -> LintIssue:
    location = raw.get("location") or {}
    return LintIssue(
        file=raw.get("filename", "unknown"),
        line=location.get("row"),
        code=raw.get("code") or "RUFF",
        message=raw.get("message", "lint violation"),
    )


class TestLint(Tool):
    name = "test.lint"
    description = (
        "Run the linter (ruff) and report violations as structured issues. "
        "Synonyms: lint, style check, ruff, code smells, static analysis."
    )
    Input = TestLintRequest
    Output = LintResult

    def run(self, request: TestLintRequest) -> LintResult:
        _require("ruff")
        proc = _run([sys.executable, "-m", "ruff", "check", "--output-format=json", request.path])
        raw = json.loads(proc.stdout) if proc.stdout.strip() else []
        issues = [_lint_issue(item) for item in raw]
        return LintResult(passed=not issues, issues=issues)


def _type_errors(output: str) -> list[TypeCheckError]:
    errors: list[TypeCheckError] = []
    for line in output.splitlines():
        parts = line.split(":", 3)
        if len(parts) >= 4 and "error" in parts[3]:
            errors.append(
                TypeCheckError(
                    file=parts[0],
                    line=int(parts[1]) if parts[1].isdigit() else None,
                    message=parts[3].split("error:", 1)[-1].strip() or "type error",
                )
            )
    return errors


class TestTypecheck(Tool):
    name = "test.typecheck"
    description = (
        "Run the type checker (mypy) and report type errors. Synonyms: mypy, types, "
        "type errors, static types, does it type-check."
    )
    Input = TestTypecheckRequest
    Output = TypecheckResult

    def run(self, request: TestTypecheckRequest) -> TypecheckResult:
        _require("mypy")
        proc = _run([sys.executable, "-m", "mypy", request.path, "--no-error-summary",
                     "--no-color-output", "--no-pretty"])
        errors = _type_errors(proc.stdout)
        return TypecheckResult(passed=not errors, errors=errors)


class TestFormat(Tool):
    name = "test.format"
    description = (
        "Run the formatter (ruff format) and report which files changed or would "
        "change. Synonyms: format, reformat, black, fix style, gofmt for python."
    )
    Input = TestFormatRequest
    Output = FormatResult

    def run(self, request: TestFormatRequest) -> FormatResult:
        _require("ruff")
        flag = ["--check"] if request.check else []
        proc = _run([sys.executable, "-m", "ruff", "format", *flag, request.path])
        changed = [
            line.split(" ", 1)[-1]
            for line in proc.stdout.splitlines()
            if line.startswith("Would reformat")
        ]
        return FormatResult(ok=proc.returncode == 0, changed=changed)


# ---- ci.* : composing the github namespace ----------------------------------
class CiTriggerCi(_GitHubTool):
    name = "ci.trigger_ci"
    description = (
        "Trigger a CI workflow run for a ref and return the resulting run id. "
        "Synonyms: dispatch workflow, kick off CI, run the pipeline, re-run CI."
    )
    Input = CiTriggerRequest
    Output = CiTriggerResult

    def run(self, request: CiTriggerRequest) -> CiTriggerResult:
        def operation() -> int:
            repo = self._repo()
            repo.get_workflow(request.workflow).create_dispatch(request.ref)
            return next(iter(repo.get_workflow_runs(branch=request.ref))).id

        run_id = self._call(operation)
        return CiTriggerResult(run_id=run_id, queued=True)


class CiGetCiStatus(_GitHubTool):
    name = "ci.get_ci_status"
    description = (
        "Fetch the status and conclusion of a CI run by its id. Synonyms: CI status, "
        "is the build green, did CI pass, workflow result, pipeline state."
    )
    Input = CiStatusRequest
    Output = CiStatusResult

    def run(self, request: CiStatusRequest) -> CiStatusResult:
        run = self._call(lambda: self._repo().get_workflow_run(request.run_id))
        return CiStatusResult(run_id=run.id, status=run.status, conclusion=run.conclusion)


# The two ci.* classes that take an injectable session, so tests can drive them
# with a fake client exactly as the github.* tools are driven.
_CI_TOOL_CLASSES = (CiTriggerCi, CiGetCiStatus)


def bind_ci_session(session: GitHubSession | None) -> dict[str, _GitHubTool]:
    """Construct the ci.* tools bound to a session (a fake one in tests)."""
    return {cls.name: cls(session) for cls in _CI_TOOL_CLASSES}


TOOLS = (
    TestRunTests(),
    TestRunSubset(),
    TestCoverage(),
    TestLint(),
    TestTypecheck(),
    TestFormat(),
    *bind_ci_session(None).values(),
)

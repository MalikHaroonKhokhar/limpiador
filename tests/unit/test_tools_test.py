"""Unit tests for the ``test.*`` / ``ci.*`` namespace — verification (HAR-21).

This is the verification half of the fix loop (ARCHITECTURE.md §8). The
load-bearing tool is ``run_tests``: it does not just say *some tests failed*, it
emits a typed :class:`TestResult` whose ``failures`` are structured
``{test, file, line, message}`` objects. That structure is what the agent
consumes to locate the cause, edit, and re-run — so the test below proves the
object handoff, not just the counts.

The toolchain tools (lint/typecheck/format) shell out to real external tools;
where the tool is absent from the environment the failure is a *typed* error the
loop can read, never a raw traceback. The ``ci.*`` tools compose the github
namespace — they route through the same injectable :class:`GitHubSession`, so
they are driven here by a fake client on a fake clock with no network.
"""

from __future__ import annotations

import ast as py_ast
import pathlib
from types import SimpleNamespace

import pytest

from limpiador.observability.errors import NotFoundError, ToolError
from limpiador.observability.retry import RateLimit, Resilience, RetryPolicy
from limpiador.schemas import (
    CiStatusResult,
    CiTriggerResult,
    CoverageResult,
    FsReadFileRequest,
    LintResult,
    TestFailure,
    TestResult,
)
from limpiador.tools import test_tools
from limpiador.tools.github_client import GitHubBoundary
from limpiador.tools.github_tools import GitHubSession
from limpiador.tools.registry import CORE_TOOL_NAMES, ToolRegistry

# The eight tools this namespace is specified to expose (ARCHITECTURE.md §5.3).
_VERIFICATION_TOOL_NAMES = (
    "test.run_tests",
    "test.run_subset",
    "test.coverage",
    "test.lint",
    "test.typecheck",
    "test.format",
    "ci.trigger_ci",
    "ci.get_ci_status",
)

_TOOLS_BY_NAME = {tool.name: tool for tool in test_tools.TOOLS}


def _tool(name: str):
    return _TOOLS_BY_NAME[name]


_SAMPLE = '''def test_passes():
    assert True


def test_fails():
    value = 2 + 2
    assert value == 5
'''


@pytest.fixture
def suite(tmp_path, monkeypatch):
    """A temp project with one passing and one failing test, cwd moved inside."""
    (tmp_path / "test_sample.py").write_text(_SAMPLE)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---- run_tests: structured failures that feed the fix loop -------------------
def test_run_tests_reports_structured_failures(suite) -> None:
    result = _tool("test.run_tests").invoke({"path": "test_sample.py"})

    assert isinstance(result, TestResult)
    assert result.passed == 1
    assert result.failed == 1
    assert result.ok is False

    failure = result.failures[0]
    assert isinstance(failure, TestFailure)
    assert "test_fails" in failure.test
    assert failure.file.endswith("test_sample.py")
    assert failure.line is not None and failure.line >= 1
    assert "5" in failure.message  # the assertion that failed: `value == 5`


def test_run_tests_failures_are_consumable_by_the_fix_loop(suite) -> None:
    # The fix loop reads a structured failure and goes to read the failing file
    # at the failing line. That is an object handoff: a TestFailure's fields
    # validate directly as the input to the fs.read_file tool — no string
    # munging, no re-discovery.
    result = _tool("test.run_tests").invoke({"path": "test_sample.py"})
    failure = result.failures[0]

    located = FsReadFileRequest(
        path=failure.file, start_line=failure.line, end_line=failure.line
    )
    assert located.path == failure.file
    assert located.start_line == failure.line


def test_a_clean_suite_reports_no_failures(tmp_path, monkeypatch) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    monkeypatch.chdir(tmp_path)

    result = _tool("test.run_tests").invoke({"path": "test_ok.py"})

    assert result.ok is True
    assert result.failed == 0
    assert result.passed == 1


# ---- run_subset -------------------------------------------------------------
def test_run_subset_runs_only_the_named_tests(suite) -> None:
    result = _tool("test.run_subset").invoke({"tests": ["test_sample.py::test_passes"]})

    assert isinstance(result, TestResult)
    assert result.passed == 1
    assert result.failed == 0


# ---- coverage (the toolchain is present) ------------------------------------
def test_coverage_reports_percentages(tmp_path, monkeypatch) -> None:
    (tmp_path / "mod.py").write_text("def used():\n    return 1\n")
    (tmp_path / "test_mod.py").write_text("from mod import used\n\ndef test_used():\n    assert used() == 1\n")
    monkeypatch.chdir(tmp_path)

    result = _tool("test.coverage").invoke({"path": "."})

    assert isinstance(result, CoverageResult)
    assert 0.0 <= result.total_percent <= 100.0
    assert any(fc.file.endswith("mod.py") for fc in result.files)


# ---- a missing toolchain is a typed error, never a traceback ----------------
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("test.lint", {"path": "."}),
        ("test.typecheck", {"path": "."}),
        ("test.format", {"path": ".", "check": True}),
    ],
)
def test_a_missing_toolchain_raises_a_typed_error(suite, name, arguments, monkeypatch) -> None:
    """An absent toolchain surfaces as a typed ToolError the loop can fold and
    adapt to — never a raw FileNotFoundError or ModuleNotFoundError.

    The absence is *staged*, not inferred from the ambient environment: ruff and
    mypy are dev dependencies now that CI gates on them (HAR-34), so a test that
    relied on them being missing would pass only by accident of the machine.
    """
    monkeypatch.setattr(test_tools.importlib.util, "find_spec", lambda module: None)

    with pytest.raises(ToolError):
        _tool(name).invoke(arguments)


def test_lint_runs_the_real_toolchain_and_reports_structured_issues(suite) -> None:
    """The other half of the contract: with ruff present, the tool really runs it
    and returns a typed LintResult rather than a string blob."""
    (suite / "messy.py").write_text("import os\n")  # F401: imported but unused

    result = _tool("test.lint").invoke({"path": "."})

    assert isinstance(result, LintResult)
    assert result.passed is False
    assert any(issue.file.endswith("messy.py") for issue in result.issues)


# ---- ci.* : composing the github namespace through a fake session -----------
def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _returns(value: object):
    return lambda *args, **kwargs: value


def _ci_session(repo: SimpleNamespace) -> GitHubSession:
    clock = _FakeClock()
    boundary = GitHubBoundary(
        resilience=Resilience(
            retry=RetryPolicy(max_attempts=4, base_delay_s=0.5),
            rate_limit=RateLimit(rate_per_second=1000.0, burst=1000),
            sleep=clock.sleep,
            clock=clock.time,
        )
    )
    return GitHubSession(boundary=boundary, client_factory=lambda: repo_client(repo), slug="octocat/limpiador")


def repo_client(repo: SimpleNamespace) -> SimpleNamespace:
    return _ns(get_repo=_returns(repo))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_trigger_ci_dispatches_and_returns_a_run_id() -> None:
    workflow = _ns(create_dispatch=_returns(True))
    repo = _ns(
        get_workflow=_returns(workflow),
        get_workflow_runs=_returns([_ns(id=42, status="queued", conclusion=None)]),
    )
    tool = test_tools.bind_ci_session(_ci_session(repo))["ci.trigger_ci"]

    result = tool.invoke({"ref": "main", "workflow": "ci.yml"})

    assert isinstance(result, CiTriggerResult)
    assert result.run_id == 42
    assert result.queued is True


def test_get_ci_status_returns_status_and_conclusion() -> None:
    repo = _ns(get_workflow_run=_returns(_ns(id=42, status="completed", conclusion="success")))
    tool = test_tools.bind_ci_session(_ci_session(repo))["ci.get_ci_status"]

    result = tool.invoke({"run_id": 42})

    assert isinstance(result, CiStatusResult)
    assert result.run_id == 42
    assert result.status == "completed"
    assert result.conclusion == "success"


def test_ci_not_found_maps_to_a_typed_error() -> None:
    from github import GithubException

    def boom(*args: object, **kwargs: object):
        raise GithubException(404, {"message": "Not Found"}, None)

    repo = _ns(get_workflow_run=boom)
    tool = test_tools.bind_ci_session(_ci_session(repo))["ci.get_ci_status"]

    with pytest.raises(NotFoundError):
        tool.invoke({"run_id": 999})


# ---- registry: all eight searchable + loadable, none in the core ------------
def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in test_tools.TOOLS:
        registry.register(tool)
    return registry


def test_namespace_exposes_exactly_the_eight_tools() -> None:
    assert tuple(tool.name for tool in test_tools.TOOLS) == _VERIFICATION_TOOL_NAMES


def test_every_verification_tool_is_loadable_and_none_are_core() -> None:
    registry = _fresh_registry()
    for name in _VERIFICATION_TOOL_NAMES:
        assert name not in CORE_TOOL_NAMES
        assert registry.load({"name": name}).loaded is True
    assert set(registry.loaded_names()) == set(_VERIFICATION_TOOL_NAMES)


def test_every_verification_tool_is_searchable() -> None:
    registry = _fresh_registry()
    for name in _VERIFICATION_TOOL_NAMES:
        verb = name.split(".", 1)[1].replace("_", " ")
        found = registry.search({"query": verb, "limit": 56}).summaries
        assert name in {summary.name for summary in found}


# ---- CLEAN_CODE: every function in the module is single-purpose and small ----
_MAX_FUNCTION_LINES = 60


def test_every_function_stays_under_the_size_budget() -> None:
    source = pathlib.Path(test_tools.__file__).read_text()
    tree = py_ast.parse(source)
    oversized = []
    for node in py_ast.walk(tree):
        if isinstance(node, (py_ast.FunctionDef, py_ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span >= _MAX_FUNCTION_LINES:
                oversized.append((node.name, span))
    assert oversized == [], f"functions over {_MAX_FUNCTION_LINES} lines: {oversized}"

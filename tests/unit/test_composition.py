"""Composition proofs — tools compose via typed handoffs (ARCHITECTURE.md §8).

Property #5 is that one tool's *typed output object* is the next tool's *typed
input object* — carried across the boundary as a Python object, validated by its
schema, never re-serialized to text and re-parsed. These tests pin the four
canonical chains by asserting the type at each seam and, where the handoff is a
direct field, asserting object *identity* (the very object the producer returned
is the one the consumer consumes):

1. ``ast.find_references`` → ``ast.rename_symbol`` — a :class:`RefList` is the
   ``references`` field of :class:`RenameSymbolRequest`, consumed unchanged.
2. ``test.run_tests`` → the fix loop — a :class:`TestFailure`'s structured
   ``file``/``line`` fields are the input to ``fs.read_file``, with no parsing of
   a traceback string.
3. ``github.get_pr`` → ``spawn_reviewer`` → ``github.comment_issue`` — a
   :class:`PullRequest`'s ``diff``/``changed_files`` feed the reviewer, whose
   :class:`ReviewResult` becomes a comment body.
4. ``ast.call_graph`` ⋈ ``ast.find_dead_code`` — two typed analyses compose into
   a consistent structural conclusion (a dead symbol is unreachable), read from
   typed fields, never from free text.

A final negative test pins the other half of the contract: feeding a mismatched
type across a seam is a validation error, so the boundary cannot be crossed with
the wrong object.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.observability.errors import MalformedInputError
from limpiador.observability.retry import RateLimit, Resilience, RetryPolicy
from limpiador.schemas import (
    AstCallGraph,
    AstDeadCodeResult,
    AstRenameResult,
    FsReadFileRequest,
    GithubCommentIssueRequest,
    PullRequest,
    RefList,
    RenameSymbolRequest,
    ReviewResult,
    TestFailure,
    TestResult,
    Verdict,
)
from limpiador.subagents.reviewer import spawn_reviewer
from limpiador.tools import ast_tools, fs_tools, github_tools, test_tools
from limpiador.tools.github_client import GitHubBoundary
from limpiador.tools.github_tools import GitHubSession

_CORE = '''from pkg.helpers import helper

CONSTANT = 10


def compute(value):
    total = helper(value)
    return total + CONSTANT


def main():
    return compute(5)
'''

_HELPERS = '''def helper(x):
    return x * 2


def unused_helper(y):
    return y
'''


def _by_name(module) -> dict:
    return {tool.name: tool for tool in module.TOOLS}


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """A small package the ast tools resolve ambiently from the cwd."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "core.py").write_text(_CORE)
    (package / "helpers.py").write_text(_HELPERS)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---- chain 1: find_references -> rename_symbol (RefList consumed directly) ----
def test_find_references_output_is_rename_symbols_input(pkg) -> None:
    refs = _by_name(ast_tools)["ast.find_references"].invoke(
        {"file": "pkg/core.py", "symbol": "compute"}
    )
    assert isinstance(refs, RefList)

    # The producer's object IS the consumer's input field — same object, no
    # re-parse, no string round-trip.
    request = RenameSymbolRequest(references=refs, new_name="calculate")
    assert request.references is refs
    assert isinstance(request.references, RefList)

    result = _by_name(ast_tools)["ast.rename_symbol"].invoke(request)
    assert isinstance(result, AstRenameResult)
    assert result.sites_changed == len(refs.references)
    assert "def calculate(value):" in (pkg / "pkg" / "core.py").read_text()


# ---- chain 2: run_tests -> fix loop (structured failure locates the cause) ----
def test_run_tests_failure_is_the_fix_loops_read_input(tmp_path, monkeypatch) -> None:
    (tmp_path / "test_buggy.py").write_text(
        "def test_off_by_one():\n    value = 2 + 2\n    assert value == 5\n"
    )
    monkeypatch.chdir(tmp_path)

    result = _by_name(test_tools)["test.run_tests"].invoke({"path": "test_buggy.py"})
    assert isinstance(result, TestResult)
    failure = result.failures[0]
    assert isinstance(failure, TestFailure)

    # The fix loop reads the structured failure's fields straight into fs.read_file
    # — locating the cause without parsing a traceback string.
    located = FsReadFileRequest(path=failure.file, start_line=failure.line, end_line=failure.line)
    assert isinstance(located, FsReadFileRequest)
    assert located.path == failure.file
    assert located.start_line == failure.line

    # ...and that input drives the read tool for real.
    content = _by_name(fs_tools)["fs.read_file"].invoke(located)
    assert "assert value == 5" in content.content


# ---- chain 3: get_pr -> spawn_reviewer -> comment_issue ----------------------
def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _pr_session() -> GitHubSession:
    clock = _Clock()
    pull = _ns(
        number=7,
        title="Fix billing",
        state="open",
        head=_ns(ref="fix"),
        base=_ns(ref="main"),
        body="why",
        get_files=_returns([_ns(filename="src/billing.py", patch="@@ -1 +1 @@\n-a\n+b")]),
    )
    repo = _ns(get_pull=_returns(pull))
    boundary = GitHubBoundary(
        resilience=Resilience(
            retry=RetryPolicy(max_attempts=2, base_delay_s=0.1),
            rate_limit=RateLimit(rate_per_second=1000.0, burst=1000),
            sleep=clock.sleep,
            clock=clock.time,
        )
    )
    return GitHubSession(boundary=boundary, client_factory=lambda: _ns(get_repo=_returns(repo)), slug="o/r")


def _returns(value: object):
    return lambda *args, **kwargs: value


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_pull_request_feeds_reviewer_whose_result_feeds_a_comment() -> None:
    get_pr = github_tools.bind_session(_pr_session())["github.get_pr"]
    pull = get_pr.invoke({"number": 7})
    assert isinstance(pull, PullRequest)
    assert pull.diff is not None  # the real diff, assembled by get_pr

    # The PR's typed fields are the reviewer's inputs — no string is parsed out.
    review = ReviewResult(verdict=Verdict.REQUEST_CHANGES, summary="inflates the total")
    mock = MockLLM(scenario(tool_turn(tool_call("finish", {"result": review.model_dump_json()}))))
    result = spawn_reviewer(diff=pull.diff, changed_files=pull.changed_files, adapter=mock)
    assert isinstance(result, ReviewResult)

    # The reviewer's typed result is the comment's input object.
    comment = GithubCommentIssueRequest(number=pull.number, body=result.summary or "review")
    assert isinstance(comment, GithubCommentIssueRequest)
    assert comment.number == pull.number
    assert comment.body == "inflates the total"


# ---- chain 4: call_graph ⋈ find_dead_code (typed structural conclusion) -------
def test_call_graph_and_dead_code_compose_into_a_consistent_conclusion(pkg) -> None:
    graph = _by_name(ast_tools)["ast.call_graph"].invoke({"symbol": "main", "depth": 3})
    dead = _by_name(ast_tools)["ast.find_dead_code"].invoke({"path": "pkg"})
    assert isinstance(graph, AstCallGraph)
    assert isinstance(dead, AstDeadCodeResult)

    # Both conclusions are read from typed fields — edge.callee and symbol.name —
    # never scraped from text. A symbol reachable from a live root is never dead.
    reachable = {edge.callee for edge in graph.edges}
    dead_names = {symbol.name for symbol in dead.symbols}
    assert "unused_helper" in dead_names
    assert reachable.isdisjoint(dead_names)
    assert "helper" in reachable and "helper" not in dead_names


# ---- the negative half of the contract: a mismatched type cannot cross --------
def test_a_mismatched_type_at_a_seam_is_a_validation_error() -> None:
    # A TestResult is not a RefList: the rename request's schema refuses it, so the
    # boundary cannot be crossed with the wrong object.
    with pytest.raises(ValidationError):
        RenameSymbolRequest(references=TestResult(passed=0, failed=0), new_name="x")


def test_handing_a_tool_the_wrong_object_is_a_typed_tool_error(pkg) -> None:
    # rename_symbol expects a RenameSymbolRequest; handing it a bare RefList (the
    # producer's object, not wrapped) is a typed MalformedInputError, not a crash.
    refs = _by_name(ast_tools)["ast.find_references"].invoke(
        {"file": "pkg/core.py", "symbol": "compute"}
    )
    with pytest.raises(MalformedInputError):
        _by_name(ast_tools)["ast.rename_symbol"].invoke(refs)

"""Mock-integration tests for the reviewer subagent (ARCHITECTURE.md §9, property #2).

A subagent is genuinely isolated, not a function call relabelled. These tests pin
all four isolation axes against the deterministic mock model:

* **Scoped registry** — the reviewer's registry is a strict, read-only *subset*
  of the parent's 56-tool registry. A write tool is not merely unused; it is
  absent — it cannot even be loaded.
* **Isolated context** — the reviewer's loop starts from only its own system
  prompt and the structured task (the PR diff + changed files); the parent's
  message history never crosses the boundary, so the first thing the reviewer's
  model sees is exactly two messages.
* **Structured return** — ``spawn_reviewer`` hands back one typed
  :class:`ReviewResult`; the reviewer's internal tool calls do not leak to the
  parent, which receives the object and nothing else.
* **Bounded** — the reviewer runs under its own call ceiling and still returns a
  typed result when it trips, rather than hanging.
"""

from __future__ import annotations

import pytest
from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.agent.guard import CallGuard
from limpiador.observability.errors import NotFoundError
from limpiador.schemas import Finding, ReviewResult, Severity, Verdict
from limpiador.subagents.reviewer import build_reviewer_registry, spawn_reviewer
from limpiador.tools.registry import REGISTRY

_DIFF = """\
--- a/src/billing.py
+++ b/src/billing.py
@@ -1,3 +1,3 @@
 def total(items):
-    return sum(i.price for i in items)
+    return sum(i.price for i in items) + 1
"""


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    """A tiny working tree so the reviewer's read-only fs tools have a real file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "billing.py").write_text("def total(items):\n    return 0\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _finish(review: ReviewResult) -> object:
    return tool_turn(tool_call("finish", {"result": review.model_dump_json()}))


# ---- the scoped registry is a strict, read-only subset of the parent ---------
def test_reviewer_registry_is_a_strict_read_only_subset_of_the_parent() -> None:
    reviewer = set(build_reviewer_registry().tool_names())
    parent = set(REGISTRY.tool_names())

    # Structural: the reviewer's tool set is a proper subset of the parent's.
    assert reviewer < parent

    # The read-only capabilities it needs are present...
    assert {"fs.read_file", "fs.grep", "ast.find_references", "github.get_pr"} <= reviewer

    # ...and nothing that writes, commits, or merges is in scope.
    for writer in (
        "fs.write_file", "fs.delete", "fs.move", "fs.apply_patch", "fs.mkdir",
        "ast.rename_symbol", "ast.extract_function",
        "git.commit", "git.stage", "git.push", "github.merge_pr", "github.create_pr",
    ):
        assert writer not in reviewer


def test_a_write_tool_cannot_even_be_loaded_into_the_reviewer_scope() -> None:
    registry = build_reviewer_registry()
    # "Unavailable" is enforced structurally: the writer is not in the registry,
    # so loading it is a typed NotFoundError, not a silent escalation of scope.
    with pytest.raises(NotFoundError):
        registry.load({"name": "fs.write_file"})


def test_the_reviewer_registry_is_a_different_object_than_the_parent() -> None:
    a = build_reviewer_registry()
    b = build_reviewer_registry()
    assert a is not REGISTRY
    assert a is not b  # each spawn gets its own isolated registry


# ---- isolated context + typed return ----------------------------------------
def test_reviewer_runs_isolated_and_returns_a_typed_review_result(worktree) -> None:
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        findings=[
            Finding(
                severity=Severity.ERROR,
                file="src/billing.py",
                line=2,
                message="off-by-one: the total is inflated by 1",
                suggestion="drop the `+ 1`",
            )
        ],
        summary="one correctness bug",
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"path": "src/billing.py"})),
            _finish(review),
        )
    )

    result = spawn_reviewer(diff=_DIFF, changed_files=["src/billing.py"], adapter=mock)

    # Structured return: exactly one typed ReviewResult.
    assert isinstance(result, ReviewResult)
    assert result.verdict == Verdict.REQUEST_CHANGES
    assert result.findings[0].file == "src/billing.py"
    assert result.findings[0].severity == Severity.ERROR

    # Isolated context: the reviewer's first model call saw only its own system
    # prompt and the structured task — two messages, no parent transcript.
    first_messages, _ = mock.received[0]
    assert [m["role"] for m in first_messages] == ["system", "user"]
    assert _DIFF in first_messages[1]["content"]


def test_internal_calls_do_not_leak_to_the_parent(worktree) -> None:
    # The reviewer reads files and could make many calls; the parent receives the
    # ReviewResult alone — there is no transcript or tool-call list hanging off it.
    review = ReviewResult(verdict=Verdict.APPROVE, summary="looks correct")
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_grep", {"pattern": "price", "path": "src"})),
            tool_turn(tool_call("fs_read_file", {"path": "src/billing.py"})),
            _finish(review),
        )
    )

    result = spawn_reviewer(diff=_DIFF, changed_files=["src/billing.py"], adapter=mock)

    assert isinstance(result, ReviewResult)
    assert result.verdict == Verdict.APPROVE
    assert not hasattr(result, "messages")
    assert not hasattr(result, "tool_calls")


# ---- bounded by its own call ceiling ----------------------------------------
def test_reviewer_respects_the_call_ceiling(worktree) -> None:
    # A scenario that never calls finish — it just keeps reading.
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("fs_read_file", {"path": "src/billing.py"})),
            tool_turn(tool_call("fs_read_file", {"path": "src/billing.py"})),
        )
    )
    guard = CallGuard(ceiling=2)

    result = spawn_reviewer(
        diff=_DIFF, changed_files=["src/billing.py"], adapter=mock, guard=guard
    )

    # Even on abort the reviewer still returns a typed result, rather than hanging
    # or raising — and it ran right up to the ceiling, no further.
    assert isinstance(result, ReviewResult)
    assert guard.calls == 2
    assert result.verdict == Verdict.COMMENT  # an incomplete review is signalled, not faked

"""The reviewer subagent — ``spawn_reviewer`` (ARCHITECTURE.md §9, property #2).

A subagent is genuinely isolated, not a function call relabelled. It is isolated
on three axes:

* **Isolated context** — :func:`spawn_reviewer` starts a *fresh* loop whose only
  inputs are its own system prompt and a task built from the structured inputs it
  is handed (the PR diff, the changed files). The parent's reasoning and tool
  history never cross the boundary, so the reviewer's judgment is not biased by
  the parent's hypotheses.
* **Scoped tool set** — the reviewer runs against a *different* registry built by
  :func:`build_reviewer_registry`, scoped to a read-only set (``fs.*`` reads, the
  ``ast.*`` analysis tools, ``github.get_pr``). The scoping is enforced at
  construction from an allow-list — a reviewer that could write, commit, or merge
  would not be a reviewer. The two mutating ``ast.*`` tools (``rename_symbol``,
  ``extract_function``) are excluded for the same reason every other writer is.
* **Structured return** — the reviewer runs its own loop to completion and
  :func:`spawn_reviewer` hands back exactly one typed :class:`ReviewResult`
  (findings with severity/file/line/suggestion plus a verdict). Its internal tool
  calls do not leak: the parent receives the object and nothing else.

``spawn_reviewer`` is deliberately *not* one of the 57 registry tools — it is the
parent's orchestration primitive for delegating a review, not a repo-acting tool.
"""

from __future__ import annotations

from pydantic import ValidationError

from limpiador.agent.guard import CallGuard
from limpiador.agent.llm import LLMAdapter
from limpiador.agent.loop import RunResult, run
from limpiador.observability.tracing import Tracer
from limpiador.schemas import ReviewResult, Verdict
from limpiador.tools.base import Tool
from limpiador.tools.registry import ToolRegistry

# The read-only capabilities the reviewer is scoped to. Enforced at construction
# (build_reviewer_registry registers only these), so write/commit/merge tools are
# structurally absent — not merely unused. The mutating ast.* tools are not here.
_REVIEWER_SCOPE: frozenset[str] = frozenset(
    {
        # fs.* — the read-only subset only (no write/move/delete/mkdir/apply_patch)
        "fs.read_file",
        "fs.list_dir",
        "fs.glob",
        "fs.grep",
        "fs.file_stat",
        # ast.* — analysis only (no rename_symbol / extract_function, which write)
        "ast.parse_file",
        "ast.list_symbols",
        "ast.find_definition",
        "ast.find_references",
        "ast.call_graph",
        "ast.dependency_tree",
        "ast.find_dead_code",
        "ast.detect_cycles",
        "ast.list_imports",
        "ast.complexity_score",
        # github.* — reading the PR under review, nothing that mutates it
        "github.get_pr",
    }
)

REVIEWER_SYSTEM_PROMPT = (
    "You are a code reviewer running in an isolated context. You have read-only "
    "tools (filesystem reads, semantic AST analysis, and github.get_pr) and you "
    "cannot write, commit, or merge — your only job is to judge the change. "
    "Investigate the diff and the changed files, then call finish exactly once "
    "with a JSON ReviewResult of the form "
    '{"verdict": "approve|request_changes|comment", "findings": '
    '[{"severity": "info|warning|error", "file": ..., "line": ..., '
    '"message": ..., "suggestion": ...}], "summary": ...}.'
)

_DEFAULT_TASK = "Review this pull request for correctness, security, and clarity."


def _read_only_tools() -> list[Tool]:
    """The constructed tool instances that fall inside the reviewer's scope."""
    from limpiador.tools import ast_tools, fs_tools, github_tools

    catalog = [*fs_tools.TOOLS, *ast_tools.TOOLS, *github_tools.TOOLS]
    return [tool for tool in catalog if tool.name in _REVIEWER_SCOPE]


def build_reviewer_registry() -> ToolRegistry:
    """A fresh registry scoped to the reviewer's read-only allow-list.

    Each spawn gets its own registry, distinct from the parent's 57-tool one.
    Tools are registered *and loaded* so the reviewer's whole (small) menu is
    immediately available — discovery-at-scale is the parent's property, not the
    reviewer's. Only allow-listed tools are registered, so a writer cannot be
    loaded into this scope: that is the construction-time enforcement.
    """
    registry = ToolRegistry()
    for tool in _read_only_tools():
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def _compose_task(task: str, diff: str, changed_files: list[str]) -> str:
    """Build the reviewer's task from the structured inputs it is handed."""
    listing = "\n".join(f"- {path}" for path in changed_files) or "(none listed)"
    return f"{task}\n\n## Changed files\n{listing}\n\n## Diff\n{diff}"


def _parse_review(outcome: RunResult) -> ReviewResult:
    """Project the reviewer's run outcome onto one typed :class:`ReviewResult`.

    A bounded (aborted) or absent result, or a finish payload that does not parse
    as a ReviewResult, becomes a COMMENT verdict that *signals* the review did not
    conclude — never a fabricated approval.
    """
    if outcome.aborted or outcome.result is None:
        return ReviewResult(
            verdict=Verdict.COMMENT,
            summary="Reviewer did not finish: the run was bounded before a verdict was reached.",
        )
    try:
        return ReviewResult.model_validate_json(outcome.result)
    except ValidationError:
        return ReviewResult(
            verdict=Verdict.COMMENT,
            summary="Reviewer returned an unstructured result; no verdict could be parsed.",
        )


def spawn_reviewer(
    *,
    diff: str,
    changed_files: list[str],
    adapter: LLMAdapter,
    task: str = _DEFAULT_TASK,
    guard: CallGuard | None = None,
    tracer: Tracer | None = None,
) -> ReviewResult:
    """Run the reviewer subagent in isolation and return one typed ReviewResult.

    The reviewer gets a fresh, read-only registry and a fresh loop seeded only
    with its system prompt and the structured task — no parent history. It runs to
    its own ``finish`` (or its own call ceiling) and the single ReviewResult is the
    only thing that crosses back to the parent.
    """
    outcome = run(
        _compose_task(task, diff, changed_files),
        registry=build_reviewer_registry(),
        adapter=adapter,
        guard=guard,
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        tracer=tracer,
    )
    return _parse_review(outcome)

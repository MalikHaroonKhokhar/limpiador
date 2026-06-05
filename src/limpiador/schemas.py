"""Typed I/O contracts for every tool (ARCHITECTURE.md §8, property #5).

Every tool consumes and emits a pydantic model defined here — never free text,
never an untyped dict. Typed I/O is what makes tool composability real: one
tool's output object is another tool's input object, validated at the boundary
(CLEAN_CODE.md §5). The canonical chains the architecture relies on are encoded
directly as types here:

* ``ast.find_references`` → :class:`RefList` → :class:`RenameSymbolRequest`
* ``test.run_tests`` → :class:`TestResult` (structured :class:`TestFailure`\\ s)
* ``github.get_pr`` → reviewer → :class:`ReviewResult` (:class:`Finding`\\ s)

The model boundary is strict (``extra='forbid'``) and value-like (``frozen``),
so a payload cannot silently grow a field as it passes from one tool to the
next, and a result is not mutated in flight.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    """Base for every limpiador I/O contract.

    ``extra='forbid'`` keeps the boundary strict — a tool cannot accept or emit
    an unexpected field — which is precisely what lets one tool's output be
    trusted as the next tool's input. ``frozen`` makes results value-like: once
    produced, a typed payload is not mutated as it crosses the loop.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# ============================================================================
# Enumerations — closed vocabularies the model and the code agree on
# ============================================================================
class Severity(str, Enum):
    """How serious a review finding is."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Verdict(str, Enum):
    """A reviewer's overall judgment on a change (ARCHITECTURE.md §9)."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


# ============================================================================
# Semantic-code contracts — the ast.* composability chain (ARCHITECTURE.md §8)
# ============================================================================
class Reference(Schema):
    """A single resolved usage of a symbol: where it appears, and which symbol."""

    file: str = Field(min_length=1, description="Repo-relative path to the file.")
    line: int = Field(ge=1, description="1-based line number of the usage.")
    symbol: str = Field(min_length=1, description="The symbol referenced at this site.")
    column: int | None = Field(default=None, ge=0, description="0-based column, if known.")


class RefList(Schema):
    """The typed output of ``ast.find_references``: every site a symbol is used.

    Consumed directly by ``ast.rename_symbol`` (:class:`RenameSymbolRequest`) —
    renaming without first consuming references is how agents miss call sites
    and break builds. An empty ``references`` list is a valid result (the symbol
    is used nowhere), not an error.
    """

    symbol: str = Field(min_length=1, description="The symbol that was searched for.")
    references: list[Reference] = Field(default_factory=list)


class FindReferencesRequest(Schema):
    """Input to ``ast.find_references``: the symbol to locate and where to anchor it."""

    file: str = Field(min_length=1, description="File the symbol is defined or used in.")
    symbol: str = Field(min_length=1, description="The symbol to find usages of.")
    line: int | None = Field(default=None, ge=1, description="Anchor line to disambiguate.")


class RenameSymbolRequest(Schema):
    """Input to ``ast.rename_symbol``: the references to edit and the new name.

    The ``references`` field is a whole :class:`RefList` — the output object of
    ``ast.find_references`` handed across unchanged. That is the composability
    contract made concrete: no re-parsing, no string passing between tools.
    """

    references: RefList
    new_name: str = Field(min_length=1, description="The replacement symbol name.")


# ============================================================================
# Verification contracts — the test.* fix loop (ARCHITECTURE.md §8)
# ============================================================================
class TestFailure(Schema):
    """A single structured test failure the agent uses to locate the cause."""

    __test__ = False  # a domain model, not a pytest test class

    test: str = Field(min_length=1, description="The failing test's identifier.")
    file: str = Field(min_length=1, description="File the failure originates in.")
    line: int | None = Field(default=None, ge=1, description="Line of the failure, if known.")
    message: str = Field(min_length=1, description="The assertion / error message.")


class TestResult(Schema):
    """The typed output of ``test.run_tests``: counts plus structured failures."""

    __test__ = False  # a domain model, not a pytest test class

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    failures: list[TestFailure] = Field(default_factory=list)
    duration_seconds: float | None = Field(default=None, ge=0)

    @property
    def ok(self) -> bool:
        """True only when nothing failed — the signal that ends the fix loop."""
        return self.failed == 0


# ============================================================================
# Review contracts — the reviewer subagent's typed return (ARCHITECTURE.md §9)
# ============================================================================
class Finding(Schema):
    """One reviewer finding: severity, location, message, and a suggested change."""

    severity: Severity
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)
    suggestion: str | None = Field(default=None, description="A concrete suggested change.")


class ReviewResult(Schema):
    """The single typed object the reviewer subagent returns to its parent."""

    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    summary: str | None = Field(default=None, description="One-line overall summary.")


# ============================================================================
# LLM-adapter contracts — the provider boundary (ARCHITECTURE.md §10)
# ============================================================================
class ToolCall(Schema):
    """A single tool call the model requested, normalized off the provider type.

    ``arguments`` is the model's raw, already-parsed call payload; it is
    validated against the target tool's typed ``Input`` at dispatch, not here.
    """

    id: str = Field(min_length=1, description="Provider-assigned call id.")
    name: str = Field(min_length=1, description="The OpenAI-safe function name.")
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(Schema):
    """Per-call token accounting (ARCHITECTURE.md §13 — tracing)."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(Schema):
    """The adapter's normalized response: free text and/or requested tool calls.

    Both the real OpenAI adapter and the mock return this exact type, so the
    loop never sees a provider object (ARCHITECTURE.md §10, .clauderules §5).
    """

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage | None = None


# ============================================================================
# git.* — local repository state (ARCHITECTURE.md §5.3, 12 tools)
# ============================================================================
class CommitInfo(Schema):
    """A single commit, as returned by git log / show / commit."""

    sha: str = Field(min_length=1)
    message: str = Field(min_length=1)
    author: str = Field(min_length=1)
    date: str | None = None


class BlameLine(Schema):
    """One blamed line: who last touched it and in which commit."""

    line: int = Field(ge=1)
    sha: str = Field(min_length=1)
    author: str = Field(min_length=1)
    content: str


class GitStatusRequest(Schema):
    """git.status takes no arguments — it reports the working tree as it is."""


class GitStatusResult(Schema):
    branch: str = Field(min_length=1)
    staged: list[str] = Field(default_factory=list)
    unstaged: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    clean: bool = True


class GitDiffRequest(Schema):
    ref: str | None = None
    path: str | None = None
    staged: bool = False


class GitDiffResult(Schema):
    diff: str
    files_changed: list[str] = Field(default_factory=list)


class GitLogRequest(Schema):
    path: str | None = None
    max_count: int = Field(default=20, ge=1)


class GitLogResult(Schema):
    commits: list[CommitInfo] = Field(default_factory=list)


class GitShowRequest(Schema):
    ref: str = Field(min_length=1)


class GitShowResult(Schema):
    commit: CommitInfo
    diff: str


class GitBranchListRequest(Schema):
    """git.branch_list takes no arguments."""


class GitBranchListResult(Schema):
    branches: list[str] = Field(default_factory=list)
    current: str = Field(min_length=1)


class GitBranchCreateRequest(Schema):
    name: str = Field(min_length=1)
    base: str | None = None


class GitBranchCreateResult(Schema):
    name: str = Field(min_length=1)
    created: bool = True


class GitCheckoutRequest(Schema):
    ref: str = Field(min_length=1)
    create: bool = False


class GitCheckoutResult(Schema):
    ref: str = Field(min_length=1)
    previous: str = Field(min_length=1)


class GitStageRequest(Schema):
    paths: list[str] = Field(min_length=1)


class GitStageResult(Schema):
    staged: list[str] = Field(default_factory=list)


class GitCommitRequest(Schema):
    message: str = Field(min_length=1)


class GitCommitResult(Schema):
    sha: str = Field(min_length=1)
    message: str = Field(min_length=1)


class GitResetRequest(Schema):
    ref: str = Field(default="HEAD", min_length=1)
    hard: bool = False


class GitResetResult(Schema):
    ref: str = Field(min_length=1)


class GitStashRequest(Schema):
    message: str | None = None
    pop: bool = False


class GitStashResult(Schema):
    stash_ref: str | None = None
    popped: bool = False


class GitBlameRequest(Schema):
    file: str = Field(min_length=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class GitBlameResult(Schema):
    file: str = Field(min_length=1)
    lines: list[BlameLine] = Field(default_factory=list)


# ============================================================================
# github.* — remote collaboration (ARCHITECTURE.md §5.3, 14 tools)
# ============================================================================
class Issue(Schema):
    number: int = Field(ge=1)
    title: str = Field(min_length=1)
    state: str = Field(min_length=1)
    body: str | None = None
    author: str | None = None
    labels: list[str] = Field(default_factory=list)


class PullRequest(Schema):
    number: int = Field(ge=1)
    title: str = Field(min_length=1)
    state: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    body: str | None = None
    diff: str | None = None
    changed_files: list[str] = Field(default_factory=list)


class CheckRun(Schema):
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    conclusion: str | None = None


class CodeMatch(Schema):
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    snippet: str


class GithubReviewSubmission(Schema):
    submitted: bool = True
    review_id: int | None = None


class GithubGetIssueRequest(Schema):
    number: int = Field(ge=1)


class GithubListIssuesRequest(Schema):
    state: str = Field(default="open", min_length=1)
    labels: list[str] = Field(default_factory=list)


class GithubIssueList(Schema):
    issues: list[Issue] = Field(default_factory=list)


class GithubCreateIssueRequest(Schema):
    title: str = Field(min_length=1)
    body: str | None = None
    labels: list[str] = Field(default_factory=list)


class GithubCommentIssueRequest(Schema):
    number: int = Field(ge=1)
    body: str = Field(min_length=1)


class GithubComment(Schema):
    id: int = Field(ge=1)
    url: str | None = None


class GithubGetPrRequest(Schema):
    number: int = Field(ge=1)


class GithubListPrsRequest(Schema):
    state: str = Field(default="open", min_length=1)


class GithubPrList(Schema):
    pull_requests: list[PullRequest] = Field(default_factory=list)


class GithubCreatePrRequest(Schema):
    title: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    body: str | None = None


class GithubReviewPrRequest(Schema):
    """github.review_pr consumes a whole ReviewResult — the reviewer's output."""

    number: int = Field(ge=1)
    review: ReviewResult


class GithubRequestChangesRequest(Schema):
    number: int = Field(ge=1)
    body: str = Field(min_length=1)


class GithubMergePrRequest(Schema):
    number: int = Field(ge=1)
    method: str = Field(default="merge", min_length=1)


class GithubMergeResult(Schema):
    merged: bool
    sha: str | None = None


class GithubListChecksRequest(Schema):
    ref: str = Field(min_length=1)


class GithubCheckList(Schema):
    checks: list[CheckRun] = Field(default_factory=list)


class GithubGetCheckLogsRequest(Schema):
    check_id: int = Field(ge=1)


class GithubCheckLogs(Schema):
    name: str = Field(min_length=1)
    logs: str


class GithubGetFileAtRefRequest(Schema):
    path: str = Field(min_length=1)
    ref: str = Field(min_length=1)


class GithubFileContent(Schema):
    path: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    content: str


class GithubSearchCodeRequest(Schema):
    query: str = Field(min_length=1)
    max_results: int = Field(default=20, ge=1)


class GithubCodeSearchResult(Schema):
    matches: list[CodeMatch] = Field(default_factory=list)


# ============================================================================
# fs.* — filesystem (ARCHITECTURE.md §5.3, 10 tools)
# ============================================================================
class DirEntry(Schema):
    name: str = Field(min_length=1)
    is_dir: bool = False


class GrepMatch(Schema):
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    text: str


class FsReadFileRequest(Schema):
    path: str = Field(min_length=1)


class FsFileContent(Schema):
    path: str = Field(min_length=1)
    content: str
    line_count: int = Field(ge=0)


class FsWriteFileRequest(Schema):
    path: str = Field(min_length=1)
    content: str


class FsWriteResult(Schema):
    path: str = Field(min_length=1)
    bytes_written: int = Field(ge=0)


class FsListDirRequest(Schema):
    path: str = Field(default=".", min_length=1)


class FsDirListing(Schema):
    path: str = Field(min_length=1)
    entries: list[DirEntry] = Field(default_factory=list)


class FsGlobRequest(Schema):
    pattern: str = Field(min_length=1)
    root: str = Field(default=".", min_length=1)


class FsGlobResult(Schema):
    matches: list[str] = Field(default_factory=list)


class FsGrepRequest(Schema):
    pattern: str = Field(min_length=1)
    path: str = Field(default=".", min_length=1)
    regex: bool = True


class FsGrepResult(Schema):
    matches: list[GrepMatch] = Field(default_factory=list)


class FsMoveRequest(Schema):
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class FsMoveResult(Schema):
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class FsDeleteRequest(Schema):
    path: str = Field(min_length=1)


class FsDeleteResult(Schema):
    path: str = Field(min_length=1)
    deleted: bool = True


class FsMkdirRequest(Schema):
    path: str = Field(min_length=1)
    parents: bool = True


class FsMkdirResult(Schema):
    path: str = Field(min_length=1)
    created: bool = True


class FsFileStatRequest(Schema):
    path: str = Field(min_length=1)


class FsFileStat(Schema):
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    is_dir: bool = False
    exists: bool = True


class FsApplyPatchRequest(Schema):
    patch: str = Field(min_length=1)


class FsApplyPatchResult(Schema):
    applied: bool
    files_changed: list[str] = Field(default_factory=list)


# ============================================================================
# ast.* — semantic code understanding (ARCHITECTURE.md §5.3, 12 tools)
# ============================================================================
class Symbol(Schema):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    file: str = Field(min_length=1)
    line: int = Field(ge=1)


class CallEdge(Schema):
    caller: str = Field(min_length=1)
    callee: str = Field(min_length=1)


class DependencyEdge(Schema):
    module: str = Field(min_length=1)
    imports: str = Field(min_length=1)


class ImportCycle(Schema):
    modules: list[str] = Field(min_length=2)


class ImportInfo(Schema):
    module: str = Field(min_length=1)
    names: list[str] = Field(default_factory=list)
    line: int | None = Field(default=None, ge=1)


class SymbolComplexity(Schema):
    symbol: str = Field(min_length=1)
    score: int = Field(ge=0)


class AstParseFileRequest(Schema):
    file: str = Field(min_length=1)


class AstParseResult(Schema):
    file: str = Field(min_length=1)
    language: str = Field(min_length=1)
    node_count: int = Field(ge=0)
    ok: bool = True


class AstListSymbolsRequest(Schema):
    file: str = Field(min_length=1)


class AstSymbolList(Schema):
    file: str = Field(min_length=1)
    symbols: list[Symbol] = Field(default_factory=list)


class AstFindDefinitionRequest(Schema):
    symbol: str = Field(min_length=1)
    file: str | None = None


class AstDefinition(Schema):
    symbol: str = Field(min_length=1)
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    kind: str = Field(min_length=1)


class AstCallGraphRequest(Schema):
    symbol: str = Field(min_length=1)
    file: str | None = None
    depth: int = Field(default=2, ge=1)


class AstCallGraph(Schema):
    root: str = Field(min_length=1)
    edges: list[CallEdge] = Field(default_factory=list)


class AstDependencyTreeRequest(Schema):
    file: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1)


class AstDependencyTree(Schema):
    root: str = Field(min_length=1)
    edges: list[DependencyEdge] = Field(default_factory=list)


class AstFindDeadCodeRequest(Schema):
    path: str = Field(default=".", min_length=1)


class AstDeadCodeResult(Schema):
    symbols: list[Symbol] = Field(default_factory=list)


class AstDetectCyclesRequest(Schema):
    path: str = Field(default=".", min_length=1)


class AstCyclesResult(Schema):
    cycles: list[ImportCycle] = Field(default_factory=list)


class AstRenameResult(Schema):
    new_name: str = Field(min_length=1)
    sites_changed: int = Field(ge=0)
    files_changed: list[str] = Field(default_factory=list)


class AstExtractFunctionRequest(Schema):
    file: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    name: str = Field(min_length=1)


class AstExtractFunctionResult(Schema):
    file: str = Field(min_length=1)
    function_name: str = Field(min_length=1)
    line: int = Field(ge=1)


class AstListImportsRequest(Schema):
    file: str = Field(min_length=1)


class AstImportList(Schema):
    file: str = Field(min_length=1)
    imports: list[ImportInfo] = Field(default_factory=list)


class AstComplexityScoreRequest(Schema):
    file: str = Field(min_length=1)
    symbol: str | None = None


class AstComplexityResult(Schema):
    file: str = Field(min_length=1)
    score: int = Field(ge=0)
    per_symbol: list[SymbolComplexity] = Field(default_factory=list)


# ============================================================================
# test.* / ci.* — verification (ARCHITECTURE.md §5.3, 8 tools)
# ============================================================================
class FileCoverage(Schema):
    file: str = Field(min_length=1)
    percent: float = Field(ge=0, le=100)


class LintIssue(Schema):
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class TypeCheckError(Schema):
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)


class TestRunRequest(Schema):
    __test__ = False  # a domain model, not a pytest test class

    path: str = Field(default=".", min_length=1)
    markers: str | None = None


class TestRunSubsetRequest(Schema):
    __test__ = False

    tests: list[str] = Field(min_length=1)


class TestCoverageRequest(Schema):
    __test__ = False

    path: str = Field(default=".", min_length=1)


class CoverageResult(Schema):
    total_percent: float = Field(ge=0, le=100)
    files: list[FileCoverage] = Field(default_factory=list)


class TestLintRequest(Schema):
    __test__ = False

    path: str = Field(default=".", min_length=1)


class LintResult(Schema):
    passed: bool
    issues: list[LintIssue] = Field(default_factory=list)


class TestTypecheckRequest(Schema):
    __test__ = False

    path: str = Field(default=".", min_length=1)


class TypecheckResult(Schema):
    passed: bool
    errors: list[TypeCheckError] = Field(default_factory=list)


class TestFormatRequest(Schema):
    __test__ = False

    path: str = Field(default=".", min_length=1)
    check: bool = False


class FormatResult(Schema):
    ok: bool = True
    changed: list[str] = Field(default_factory=list)


class CiTriggerRequest(Schema):
    ref: str = Field(min_length=1)
    workflow: str | None = None


class CiTriggerResult(Schema):
    run_id: int = Field(ge=1)
    queued: bool = True


class CiStatusRequest(Schema):
    run_id: int = Field(ge=1)


class CiStatusResult(Schema):
    run_id: int = Field(ge=1)
    status: str = Field(min_length=1)
    conclusion: str | None = None

"""Unit tests for the typed I/O contracts (ARCHITECTURE.md §8, property #5).

These prove the contract that makes tool composability real rather than
retrofitted: every model round-trips losslessly, one tool's output object
validates as the next tool's input object, and the boundary rejects malformed
payloads instead of silently accepting them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from limpiador import schemas as S
from limpiador.tools.base import NAMESPACES
from limpiador.schemas import (
    Finding,
    FindReferencesRequest,
    LLMResponse,
    Reference,
    RefList,
    RenameSymbolRequest,
    ReviewResult,
    Severity,
    TestFailure,
    TestResult,
    TokenUsage,
    ToolCall,
    Verdict,
)

# One representative instance of every contract, including the empty/default
# variants. The round-trip tests run over all of them.
SAMPLES = [
    Reference(file="billing.py", line=40, symbol="calculate_total", column=4),
    RefList(
        symbol="calculate_total",
        references=[
            Reference(file="billing.py", line=40, symbol="calculate_total"),
            Reference(file="checkout.py", line=12, symbol="calculate_total"),
        ],
    ),
    RefList(symbol="orphan"),  # zero references is a valid result
    FindReferencesRequest(file="billing.py", symbol="calculate_total", line=40),
    RenameSymbolRequest(
        references=RefList(
            symbol="calculate_total",
            references=[Reference(file="billing.py", line=40, symbol="calculate_total")],
        ),
        new_name="compute_total",
    ),
    TestFailure(test="test_totals", file="test_billing.py", line=10, message="AssertionError"),
    TestResult(
        passed=3,
        failed=1,
        failures=[TestFailure(test="test_totals", file="t.py", line=10, message="boom")],
        duration_seconds=1.2,
    ),
    TestResult(passed=0, failed=0),  # an empty run
    Finding(severity=Severity.ERROR, file="a.py", line=5, message="off-by-one", suggestion="use <="),
    Finding(severity=Severity.INFO, file="b.py", message="nit"),  # no line, no suggestion
    ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        findings=[Finding(severity=Severity.WARNING, file="a.py", line=1, message="x")],
        summary="one blocking issue",
    ),
    ReviewResult(verdict=Verdict.APPROVE),  # clean review, no findings
    ToolCall(id="call_1", name="ast_find_references", arguments={"file": "a.py", "symbol": "x"}),
    TokenUsage(prompt_tokens=1200, completion_tokens=80),
    LLMResponse(
        text="working on it",
        tool_calls=[ToolCall(id="c1", name="git_status", arguments={})],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=2),
    ),
    LLMResponse(),  # all defaults — a content-free turn
]


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_python_round_trip_is_lossless(model: object) -> None:
    """model_dump → model_validate reproduces an equal instance."""
    restored = type(model).model_validate(model.model_dump())
    assert restored == model


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_json_round_trip_is_lossless(model: object) -> None:
    """model_dump_json → model_validate_json reproduces an equal instance."""
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_reflist_output_constructs_rename_input() -> None:
    """A find_references RefList is consumed directly as rename_symbol input."""
    references = RefList(
        symbol="calculate_total",
        references=[Reference(file="billing.py", line=40, symbol="calculate_total")],
    )

    request = RenameSymbolRequest(references=references, new_name="compute_total")

    assert request.references == references
    assert request.new_name == "compute_total"


def test_reflist_output_validates_as_rename_input_over_the_wire() -> None:
    """The serialized RefList also validates as rename input (the wire contract)."""
    references = RefList(
        symbol="calculate_total",
        references=[Reference(file="billing.py", line=40, symbol="calculate_total")],
    )

    request = RenameSymbolRequest.model_validate(
        {"references": references.model_dump(), "new_name": "compute_total"}
    )

    assert request.references == references


def test_empty_string_field_is_rejected() -> None:
    """A required string cannot be empty — an empty path is not a location."""
    with pytest.raises(ValidationError):
        Reference(file="", line=40, symbol="calculate_total")


def test_non_positive_line_is_rejected() -> None:
    """Source lines are 1-based; line 0 is malformed input, not a default."""
    with pytest.raises(ValidationError):
        Reference(file="billing.py", line=0, symbol="calculate_total")


def test_negative_count_is_rejected() -> None:
    """A test run cannot have a negative passed/failed count."""
    with pytest.raises(ValidationError):
        TestResult(passed=-1, failed=0)


def test_unknown_field_is_rejected() -> None:
    """extra='forbid' keeps the boundary strict so composition stays trustworthy."""
    with pytest.raises(ValidationError):
        Reference(file="a.py", line=1, symbol="x", bogus=True)


def test_optional_fields_default_rather_than_require() -> None:
    """Genuinely optional fields default; they are not required boundaries."""
    assert RefList(symbol="x").references == []
    finding = Finding(severity=Severity.INFO, file="a.py", message="nit")
    assert finding.line is None
    assert finding.suggestion is None
    assert LLMResponse().tool_calls == []


def test_test_result_ok_reflects_failures() -> None:
    """The derived ok flag is true only when nothing failed."""
    assert TestResult(passed=2, failed=0).ok is True
    assert (
        TestResult(
            passed=2,
            failed=1,
            failures=[TestFailure(test="t", file="t.py", line=1, message="boom")],
        ).ok
        is False
    )


def test_test_result_rejects_inconsistent_failed_count() -> None:
    """A failed count that disagrees with the structured failure list is rejected."""
    with pytest.raises(ValidationError):
        TestResult(passed=0, failed=2, failures=[])
    with pytest.raises(ValidationError):
        TestResult(
            passed=0,
            failed=0,
            failures=[TestFailure(test="t", file="t.py", line=1, message="boom")],
        )


# ============================================================================
# Coverage of ARCHITECTURE.md §5.3 — every listed tool has typed I/O
# ============================================================================
# Ground truth: the 56 tools across the five namespaces (§5.3). Kept here as the
# spec the schemas are checked against, so a tool added to the architecture
# without an I/O contract fails this test.
TOOL_IO: dict[str, tuple[type[S.Schema], type[S.Schema]]] = {
    # git.* (12)
    "git.status": (S.GitStatusRequest, S.GitStatusResult),
    "git.diff": (S.GitDiffRequest, S.GitDiffResult),
    "git.log": (S.GitLogRequest, S.GitLogResult),
    "git.show": (S.GitShowRequest, S.GitShowResult),
    "git.branch_list": (S.GitBranchListRequest, S.GitBranchListResult),
    "git.branch_create": (S.GitBranchCreateRequest, S.GitBranchCreateResult),
    "git.checkout": (S.GitCheckoutRequest, S.GitCheckoutResult),
    "git.stage": (S.GitStageRequest, S.GitStageResult),
    "git.commit": (S.GitCommitRequest, S.GitCommitResult),
    "git.reset": (S.GitResetRequest, S.GitResetResult),
    "git.stash": (S.GitStashRequest, S.GitStashResult),
    "git.blame": (S.GitBlameRequest, S.GitBlameResult),
    # github.* (14)
    "github.get_issue": (S.GithubGetIssueRequest, S.Issue),
    "github.list_issues": (S.GithubListIssuesRequest, S.GithubIssueList),
    "github.create_issue": (S.GithubCreateIssueRequest, S.Issue),
    "github.comment_issue": (S.GithubCommentIssueRequest, S.GithubComment),
    "github.get_pr": (S.GithubGetPrRequest, S.PullRequest),
    "github.list_prs": (S.GithubListPrsRequest, S.GithubPrList),
    "github.create_pr": (S.GithubCreatePrRequest, S.PullRequest),
    "github.review_pr": (S.GithubReviewPrRequest, S.GithubReviewSubmission),
    "github.request_changes": (S.GithubRequestChangesRequest, S.GithubReviewSubmission),
    "github.merge_pr": (S.GithubMergePrRequest, S.GithubMergeResult),
    "github.list_checks": (S.GithubListChecksRequest, S.GithubCheckList),
    "github.get_check_logs": (S.GithubGetCheckLogsRequest, S.GithubCheckLogs),
    "github.get_file_at_ref": (S.GithubGetFileAtRefRequest, S.GithubFileContent),
    "github.search_code": (S.GithubSearchCodeRequest, S.GithubCodeSearchResult),
    # fs.* (10)
    "fs.read_file": (S.FsReadFileRequest, S.FsFileContent),
    "fs.write_file": (S.FsWriteFileRequest, S.FsWriteResult),
    "fs.list_dir": (S.FsListDirRequest, S.FsDirListing),
    "fs.glob": (S.FsGlobRequest, S.FsGlobResult),
    "fs.grep": (S.FsGrepRequest, S.FsGrepResult),
    "fs.move": (S.FsMoveRequest, S.FsMoveResult),
    "fs.delete": (S.FsDeleteRequest, S.FsDeleteResult),
    "fs.mkdir": (S.FsMkdirRequest, S.FsMkdirResult),
    "fs.file_stat": (S.FsFileStatRequest, S.FsFileStat),
    "fs.apply_patch": (S.FsApplyPatchRequest, S.FsApplyPatchResult),
    # ast.* (12)
    "ast.parse_file": (S.AstParseFileRequest, S.AstParseResult),
    "ast.list_symbols": (S.AstListSymbolsRequest, S.AstSymbolList),
    "ast.find_definition": (S.AstFindDefinitionRequest, S.AstDefinition),
    "ast.find_references": (S.FindReferencesRequest, S.RefList),
    "ast.call_graph": (S.AstCallGraphRequest, S.AstCallGraph),
    "ast.dependency_tree": (S.AstDependencyTreeRequest, S.AstDependencyTree),
    "ast.find_dead_code": (S.AstFindDeadCodeRequest, S.AstDeadCodeResult),
    "ast.detect_cycles": (S.AstDetectCyclesRequest, S.AstCyclesResult),
    "ast.rename_symbol": (S.RenameSymbolRequest, S.AstRenameResult),
    "ast.extract_function": (S.AstExtractFunctionRequest, S.AstExtractFunctionResult),
    "ast.list_imports": (S.AstListImportsRequest, S.AstImportList),
    "ast.complexity_score": (S.AstComplexityScoreRequest, S.AstComplexityResult),
    # test.* / ci.* (8)
    "test.run_tests": (S.TestRunRequest, S.TestResult),
    "test.run_subset": (S.TestRunSubsetRequest, S.TestResult),
    "test.coverage": (S.TestCoverageRequest, S.CoverageResult),
    "test.lint": (S.TestLintRequest, S.LintResult),
    "test.typecheck": (S.TestTypecheckRequest, S.TypecheckResult),
    "test.format": (S.TestFormatRequest, S.FormatResult),
    "ci.trigger_ci": (S.CiTriggerRequest, S.CiTriggerResult),
    "ci.get_ci_status": (S.CiStatusRequest, S.CiStatusResult),
}

# Every distinct I/O model, deduped (a few outputs are shared across tools).
_IO_MODELS = sorted(
    {model for pair in TOOL_IO.values() for model in pair}, key=lambda c: c.__name__
)
_DEFAULT_CONSTRUCTIBLE = [
    model
    for model in _IO_MODELS
    if not any(field.is_required() for field in model.model_fields.values())
]


def test_tool_io_covers_every_section_5_3_tool() -> None:
    """All 56 §5.3 tools — across all five namespaces — have a typed I/O pair."""
    assert len(TOOL_IO) == 56
    assert {name.split(".", 1)[0] for name in TOOL_IO} <= set(NAMESPACES)


def test_every_tool_io_is_a_pair_of_schema_subclasses() -> None:
    for name, (input_model, output_model) in TOOL_IO.items():
        assert issubclass(input_model, S.Schema), f"{name} input"
        assert issubclass(output_model, S.Schema), f"{name} output"


@pytest.mark.parametrize("model", _IO_MODELS, ids=lambda m: m.__name__)
def test_io_model_emits_a_strict_object_schema(model: type[S.Schema]) -> None:
    """Each model serializes to a strict JSON-schema object (OpenAI-ready)."""
    json_schema = model.model_json_schema()
    assert json_schema["type"] == "object"
    assert json_schema.get("additionalProperties") is False


@pytest.mark.parametrize("model", _DEFAULT_CONSTRUCTIBLE, ids=lambda m: m.__name__)
def test_default_constructible_io_model_round_trips(model: type[S.Schema]) -> None:
    """Models whose fields all default round-trip with no hand-built fixture."""
    instance = model()
    assert type(instance).model_validate(instance.model_dump()) == instance

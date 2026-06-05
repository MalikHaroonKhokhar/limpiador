"""Unit tests for the typed I/O contracts (ARCHITECTURE.md §8, property #5).

These prove the contract that makes tool composability real rather than
retrofitted: every model round-trips losslessly, one tool's output object
validates as the next tool's input object, and the boundary rejects malformed
payloads instead of silently accepting them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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
    assert TestResult(passed=2, failed=1).ok is False

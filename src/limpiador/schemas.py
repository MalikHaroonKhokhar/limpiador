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

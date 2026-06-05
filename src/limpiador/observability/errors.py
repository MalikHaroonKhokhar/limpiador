"""Typed error hierarchy (ARCHITECTURE.md §13, CLEAN_CODE.md §6).

Failures are raised as typed errors — never signalled with ``None`` or ``False``,
never hidden behind a broad ``except``. Every one serializes to a structured,
model-readable payload via :meth:`ModelReadableError.as_tool_result`: the agent
loop folds a recoverable failure back into context as that payload, so the model
can read "file not found" and adapt — it cannot adapt to a stack trace. The
subclasses distinguish failure *kind* (and carry ``recoverable`` / ``retryable``
flags) so the loop can decide whether to retry, re-route, or give up.
"""

from __future__ import annotations


class ModelReadableError(Exception):
    """An error that serializes to a structured, model-readable result.

    Every failure in limpiador is one of these. ``recoverable`` says whether the
    agent loop can fold it back and continue; ``retryable`` says whether the same
    call might succeed if simply retried (a transient blip). The payload carries
    the error's type name and message — never a bare string or a silent sentinel.
    """

    recoverable: bool = True
    retryable: bool = False

    def as_tool_result(self) -> dict[str, object]:
        """The structured payload folded into context in place of a stack trace."""
        return {
            "error": type(self).__name__,
            "message": str(self),
            "recoverable": self.recoverable,
            "retryable": self.retryable,
        }


class ToolError(ModelReadableError):
    """Base of every recoverable tool failure.

    The agent loop catches a ``ToolError`` and folds ``as_tool_result()`` back
    into context as a recoverable result the model can read and adapt to.
    """


class NotFoundError(ToolError):
    """A referenced resource (file, symbol, issue, git ref) does not exist."""


class PermissionDeniedError(ToolError):
    """The operation is not permitted — e.g. a scoped subagent attempting a write."""


class TransientError(ToolError):
    """A retryable failure: a network blip or a rate-limit response (see retry.py)."""

    retryable = True


class MalformedInputError(ToolError):
    """The tool was called with input that fails its typed contract (schemas.py)."""


class ToolUnavailableError(ToolError):
    """A declared tool exists in the registry but its executor is not implemented."""


class ConfigError(ModelReadableError):
    """A fatal configuration problem — e.g. a required key like ``OPENAI_API_KEY``
    is absent.

    Deliberately *not* a ``ToolError``: it is not an agent-recoverable failure to
    fold back into context, but a startup misconfiguration the operator must fix.
    It still serializes (with ``recoverable=False``) so a surfaced config failure
    is structured rather than a crash — but the loop never folds it; it aborts.
    """

    recoverable = False

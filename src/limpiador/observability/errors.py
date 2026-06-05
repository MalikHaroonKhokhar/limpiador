"""Typed error hierarchy (ARCHITECTURE.md §13, CLEAN_CODE.md §6).

Failures are raised as typed ``ToolError``s — never signalled with ``None`` or
``False``, never hidden behind a broad ``except``. The agent loop catches a
``ToolError`` and folds it back into context as a structured, recoverable
result: the model can read "file not found" and adapt; it cannot adapt to a
stack trace. The subclasses below distinguish failure *kind* so the loop can
decide whether to retry, re-route, or give up.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base of every recoverable tool failure."""


class NotFoundError(ToolError):
    """A referenced resource (file, symbol, issue, git ref) does not exist."""


class PermissionDeniedError(ToolError):
    """The operation is not permitted — e.g. a scoped subagent attempting a write."""


class TransientError(ToolError):
    """A retryable failure: a network blip or a rate-limit response (see retry.py)."""


class MalformedInputError(ToolError):
    """The tool was called with input that fails its typed contract (schemas.py)."""

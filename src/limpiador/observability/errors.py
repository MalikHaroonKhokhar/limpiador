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


class ToolUnavailableError(ToolError):
    """A declared tool exists in the registry but its executor is not implemented."""


class ConfigError(Exception):
    """A fatal configuration problem — e.g. a required key like ``OPENAI_API_KEY``
    is absent.

    Deliberately *not* a ``ToolError``: it is not an agent-recoverable failure to
    fold back into context, but a startup misconfiguration the operator must fix.
    Raising it (instead of crashing on a missing env var) keeps the failure typed
    and legible.
    """

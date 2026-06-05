"""Typed I/O contracts for every tool (ARCHITECTURE.md §8, property #5).

Every tool consumes and emits a pydantic model defined here — never free text,
never an untyped dict. Typed I/O is what makes tool composability real: one
tool's output object is another tool's input object, validated at the boundary
(CLEAN_CODE.md §5). The concrete contracts (``ReviewResult``, ``TestResult``,
reference lists, …) land alongside the tickets that introduce the tools that
use them; this module defines the shared base they all inherit.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Schema(BaseModel):
    """Base for every limpiador I/O contract.

    ``extra='forbid'`` keeps the boundary strict — a tool cannot silently accept
    or emit an unexpected field — which is precisely what lets one tool's output
    be trusted as the next tool's input. ``frozen`` makes results value-like:
    once produced, a typed payload is not mutated as it is passed across the
    loop and into the next tool.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

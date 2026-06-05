"""The call-count kill-switch (ARCHITECTURE.md §6, step 1).

Before each turn, the guard verifies the run has not exceeded its hard ceiling
of tool calls; if it has, the run terminates with a typed ``RunAborted`` result.
This is loop-safety, not budget accounting — a long-horizon agent that loses
coherence fails by looping, and the ceiling is how that failure mode is bounded.
The ceiling is named configuration (CLEAN_CODE.md §7), not a literal in the loop.
"""

from __future__ import annotations

# The hard ceiling on tool calls in a single run. Named configuration, not a
# literal buried in the loop (CLEAN_CODE.md §7): it is the bound on the
# lose-coherence-and-loop failure mode, not a cost budget, so it is generous
# enough for a real twenty-plus-call investigation yet finite.
DEFAULT_CALL_CEILING = 50


class RunAborted(Exception):
    """The run hit its hard tool-call ceiling and was stopped (loop safety).

    Typed so the loop terminates deliberately — surfacing "this run was bounded,
    not finished" — rather than running away or dying on an unhandled error.
    """


class CallGuard:
    """Counts tool calls across a run and trips at a fixed ceiling.

    The loop records every dispatched tool call and calls :meth:`check` at the
    top of each turn; once the accumulated count reaches the ceiling, ``check``
    raises :class:`RunAborted`. The guard tracks only a count — it has no opinion
    about which tools ran or what they cost.
    """

    def __init__(self, *, ceiling: int = DEFAULT_CALL_CEILING) -> None:
        self._ceiling = ceiling
        self._calls = 0

    @property
    def ceiling(self) -> int:
        return self._ceiling

    @property
    def calls(self) -> int:
        return self._calls

    def record(self, count: int = 1) -> None:
        """Account for ``count`` tool calls that were just dispatched."""
        self._calls += count

    def check(self) -> None:
        """Raise :class:`RunAborted` if the run has reached its ceiling."""
        if self._calls >= self._ceiling:
            raise RunAborted(
                f"run aborted: reached the hard ceiling of {self._ceiling} tool calls."
            )

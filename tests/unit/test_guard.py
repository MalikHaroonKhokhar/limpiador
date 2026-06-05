"""Unit tests for the call-count kill-switch (ARCHITECTURE.md §6, .clauderules §6).

The guard is loop-safety, not budget accounting: a long-horizon agent that loses
coherence fails by *looping*, and the hard ceiling is how that failure mode is
bounded. These tests pin the boundary exactly — the run is allowed right up to
the ceiling and aborts the moment it is reached — and prove the ceiling is named
configuration, not a literal buried in the loop.
"""

from __future__ import annotations

import pytest

from limpiador.agent.guard import DEFAULT_CALL_CEILING, CallGuard, RunAborted


def test_a_fresh_guard_passes() -> None:
    CallGuard(ceiling=3).check()  # no calls recorded yet → no abort


def test_guard_passes_below_the_ceiling() -> None:
    guard = CallGuard(ceiling=3)
    guard.record()
    guard.record()
    guard.check()  # two of three used → still under the ceiling
    assert guard.calls == 2


def test_guard_passes_at_one_below_the_ceiling() -> None:
    guard = CallGuard(ceiling=3)
    guard.record(2)
    guard.check()  # exactly ceiling - 1 → the last permitted state


def test_guard_aborts_exactly_at_the_ceiling() -> None:
    guard = CallGuard(ceiling=3)
    guard.record(3)
    with pytest.raises(RunAborted):
        guard.check()


def test_guard_aborts_past_the_ceiling() -> None:
    guard = CallGuard(ceiling=3)
    guard.record(5)
    with pytest.raises(RunAborted):
        guard.check()


def test_record_accumulates_call_counts() -> None:
    guard = CallGuard(ceiling=10)
    guard.record()
    guard.record(3)
    assert guard.calls == 4


def test_run_aborted_message_names_the_ceiling() -> None:
    guard = CallGuard(ceiling=7)
    guard.record(7)
    with pytest.raises(RunAborted, match="7"):
        guard.check()


def test_the_default_ceiling_is_named_configuration() -> None:
    assert isinstance(DEFAULT_CALL_CEILING, int)
    assert DEFAULT_CALL_CEILING > 0
    assert CallGuard().ceiling == DEFAULT_CALL_CEILING

"""Observed behaviour: the agent edited code to "fix" a failing test and then
called ``finish`` WITHOUT running the tests — declaring success it never
verified. The verification half of the fix loop (ARCHITECTURE.md §8) is exactly
this: after an edit, run the tests and read the structured result before
concluding.

Run trace: ``traces/2026-06-06/fix-without-verify.jsonl`` — a real-model dev run
where the agent patched ``calc.py`` and finished without a single
``test.run_tests`` call.

🔴 Before the HAR-28 fix, nothing told the agent to verify, so it edited and
   declared victory.
🟢 After the fix, the system prompt instructs it to verify edits by running the
   tests, and a ``test.run_tests`` call appears in the run.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.reproduce

_BUGGY = "def add(a, b):\n    return a - b\n"  # the bug: subtraction
_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_an_edit_is_verified_by_running_the_tests(make_git_repo, run_agent) -> None:
    make_git_repo({"calc.py": _BUGGY, "test_calc.py": _TEST})

    result = run_agent("Fix the failing test in this repository.", ceiling=20)

    calls = list(result.tool_calls)
    # Relaxed: the behaviour under test is that the agent verified at all — it ran
    # the tests rather than editing and declaring success blindly.
    assert "test_run_tests" in calls, (
        f"expected the agent to verify the edit by running the tests; calls were {calls}"
    )

"""Structural unit guards for the agent loop (ARCHITECTURE.md §6, .clauderules §5).

The loop's defining property is that it does *not* branch on tool identity: it
recognizes only the ``finish`` protocol verb and delegates every other call to
``registry.dispatch()``. The behavioral proof of the turn cycle lives in
``tests/integration/test_loop_mock.py``; this is the cheap source-level backstop
that keeps the spine from quietly growing a per-tool special case — the
fifty-conditional anti-pattern the brief warns against. If a concrete tool name
ever appears in the loop, that has happened, and this fails.
"""

from __future__ import annotations

import pathlib

from limpiador.tools.registry import FINISH

_LOOP_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "limpiador" / "agent" / "loop.py"
)

# A spread across every namespace — both the OpenAI-safe wire names the loop
# would see and the dotted/internal forms — so a special-case slips past nowhere.
_CONCRETE_TOOL_NAMES = (
    "fs_read_file",
    "fs_write_file",
    "ast_find_references",
    "ast_rename_symbol",
    "git_status",
    "github_get_pr",
    "test_run_tests",
    "find_references",
    "rename_symbol",
    "run_tests",
    "apply_patch",
)


def test_the_loop_names_no_concrete_tool() -> None:
    source = _LOOP_SRC.read_text()
    offenders = [name for name in _CONCRETE_TOOL_NAMES if name in source]
    assert offenders == [], (
        f"agent/loop.py must not name concrete tools (found {offenders}); "
        "dispatch is delegated to registry.dispatch()."
    )


def test_the_only_recognized_verb_is_finish_via_the_shared_constant() -> None:
    """The one verb the loop may know — and only through the registry constant.

    Recognizing ``finish`` is how a turn cycle ends, the way a function
    recognizes ``return``. The loop refers to it through the registry's named
    ``FINISH`` constant, never a bare ``"finish"`` string literal, so the wire
    name has a single source of truth.
    """
    source = _LOOP_SRC.read_text()
    assert "FINISH" in source  # the verb is referenced through the constant
    assert f'"{FINISH}"' not in source  # never as a hardcoded literal
    assert f"'{FINISH}'" not in source

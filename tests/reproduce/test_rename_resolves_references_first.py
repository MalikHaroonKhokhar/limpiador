"""Observed behaviour: the agent renamed a symbol with ``ast.rename_symbol``
WITHOUT first resolving its uses with ``ast.find_references`` — a blind rename
that risks missing call sites and breaking the build. The safe path is the
canonical chain (ARCHITECTURE.md §8): resolve references, *then* rename the
sites that resolution found.

Run trace: ``traces/2026-06-06/rename-without-refs.jsonl`` — a real-model dev
run on a two-call repo where the agent went straight to ``ast.rename_symbol``.

🔴 Before the HAR-28 fix, the minimal system prompt ("discover tools, act,
   finish") gave the model no steer, so it often renamed without resolving first.
🟢 After the fix, the system prompt instructs the agent to resolve a symbol's
   references before renaming it, and the resolve precedes the rename.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.reproduce

_SOURCE = '''def discount(price):
    return price * 0.9


def checkout(price):
    return discount(price)
'''


def test_rename_resolves_references_before_renaming(make_git_repo, run_agent) -> None:
    make_git_repo({"shop.py": _SOURCE})

    result = run_agent(
        "Rename the function `discount` to `apply_discount` everywhere in shop.py "
        "and keep the code consistent.",
        ceiling=18,
    )

    calls = list(result.tool_calls)
    # Relaxed to the behaviour that matters, not an exact transcript: the agent
    # resolved references at all (it investigated before acting)...
    assert "ast_find_references" in calls, (
        f"expected the agent to resolve references before renaming; calls were {calls}"
    )
    # ...and if it did rename, the resolve came first — never a blind rename.
    if "ast_rename_symbol" in calls:
        assert calls.index("ast_find_references") < calls.index("ast_rename_symbol"), (
            f"rename must follow reference resolution; calls were {calls}"
        )

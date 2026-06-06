"""End-to-end composition on the mock loop (ARCHITECTURE.md §6, §8, property #5).

The unit composition tests pin the typed handoff at each seam in isolation; this
one proves the canonical chain *runs to completion through the real loop*: the
orchestration spine drives find_references → rename_symbol → run_tests → finish,
each tool acting on a real temp repo, and the rename actually lands on disk.

The reference list the rename consumes is the *real* output of find_references
(computed in setup and scripted verbatim into the rename call), so the chain that
runs is the genuine producer→consumer handoff, not a hand-faked argument.
"""

from __future__ import annotations

from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

import pytest

from limpiador.agent.loop import run
from limpiador.tools import ast_tools, test_tools
from limpiador.tools.registry import ToolRegistry

_CORE = '''CONSTANT = 10


def compute(value):
    return value + CONSTANT


def main():
    return compute(5)
'''

_PASSING_TEST = "def test_smoke():\n    assert True\n"


def _by_name(module) -> dict:
    return {tool.name: tool for tool in module.TOOLS}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "core.py").write_text(_CORE)
    (tmp_path / "test_smoke.py").write_text(_PASSING_TEST)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _registry_with(*tools) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
        registry.load({"name": tool.name})
    return registry


def test_the_find_refs_rename_run_tests_chain_runs_through_the_loop(repo) -> None:
    ast = _by_name(ast_tools)
    tests = _by_name(test_tools)

    # The real producer output the rename will consume — scripted verbatim, so the
    # loop runs the genuine typed handoff rather than a hand-faked argument.
    refs = ast["ast.find_references"].invoke({"file": "pkg/core.py", "symbol": "compute"})

    registry = _registry_with(
        ast["ast.find_references"], ast["ast.rename_symbol"], tests["test.run_tests"]
    )
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("ast_find_references", {"file": "pkg/core.py", "symbol": "compute"})),
            tool_turn(tool_call("ast_rename_symbol", {"references": refs.model_dump(), "new_name": "calculate"})),
            tool_turn(tool_call("test_run_tests", {"path": "test_smoke.py"})),
            tool_turn(tool_call("finish", {"result": "renamed compute -> calculate; tests green"})),
        )
    )

    result = run("rename compute to calculate and verify", registry=registry, adapter=mock)

    # The loop drove the whole chain to its terminal finish, in order.
    assert result.aborted is False
    assert result.result == "renamed compute -> calculate; tests green"
    assert list(result.tool_calls) == [
        "ast_find_references",
        "ast_rename_symbol",
        "test_run_tests",
        "finish",
    ]

    # The rename actually landed on disk — the chain had a real effect, end to end.
    rewritten = (repo / "pkg" / "core.py").read_text()
    assert "def calculate(value):" in rewritten
    assert "return calculate(5)" in rewritten
    assert "def compute(" not in rewritten

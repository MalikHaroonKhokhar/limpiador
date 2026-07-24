"""Mock-integration tests for the CLI entrypoint (ARCHITECTURE.md §6, §12).

These drive limpiador the way `make dev-mock` does — through `cli.main`, in mock
mode — and assert the *plumbing*: arg parsing → adapter selection → loop → a
structured result → an exit code. The agent's reasoning is not under test here
(that is the eval harness, real mode); what is under test is that a scripted run
reaches `finish` and exits 0, that `LIMPIADOR_LLM=mock` actually selects the
mock, and that a guarded abort surfaces as a non-zero exit.

The scenarios drive only the core meta-tools (search/load/finish), so they run
against the real default REGISTRY end-to-end without needing tool executors.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support.mock_llm import MockLLM, scenario, tool_call, tool_turn

from limpiador.cli import main

_TESTS_DIR = Path(__file__).resolve().parents[1]  # tests/
_REPO_ROOT = _TESTS_DIR.parent


def test_dev_mock_runs_a_scripted_scenario_end_to_end(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("search_tools", {"query": "find references"})),
            tool_turn(tool_call("load_tool", {"name": "ast.find_references"})),
            tool_turn(tool_call("finish", {"result": "investigated; nothing to change"})),
        )
    )

    code = main(["run", "--repo", str(tmp_path), "--task", "look around"], adapter=mock)

    assert code == 0
    assert "investigated; nothing to change" in capsys.readouterr().out


def test_mock_mode_is_selected_by_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """LIMPIADOR_LLM=mock selects the registered mock with no adapter injected."""
    monkeypatch.setenv("LIMPIADOR_LLM", "mock")

    code = main(["run", "--repo", str(tmp_path), "--task", "anything"])

    assert code == 0  # the default mock returns a final turn → loop terminates


def test_run_aborted_surfaces_as_a_nonzero_exit(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A scenario that never calls finish, with a ceiling it will hit.
    mock = MockLLM(
        scenario(
            tool_turn(tool_call("search_tools", {"query": "a"})),
            tool_turn(tool_call("search_tools", {"query": "b"})),
        )
    )

    code = main(
        ["run", "--repo", str(tmp_path), "--task", "loop forever", "--max-calls", "2"],
        adapter=mock,
    )

    assert code != 0
    assert "abort" in capsys.readouterr().err.lower()


def test_a_missing_repo_path_is_a_clear_nonzero_exit(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock = MockLLM(scenario(tool_turn(tool_call("finish", {"result": "x"}))))
    missing = tmp_path / "does_not_exist"

    code = main(["run", "--repo", str(missing), "--task", "t"], adapter=mock)

    assert code != 0
    assert str(missing) in capsys.readouterr().err


def test_make_dev_mock_works_as_a_standalone_process(tmp_path) -> None:
    """`make dev-mock` runs a *real* process: the mock must be selectable there.

    Production never imports the mock, so a bare `python -m limpiador.cli` cannot
    resolve LIMPIADOR_LLM=mock. The dev seam is `python -m support`: importing the
    test-support package registers the mock, then the real CLI runs unchanged.
    This drives that exact entrypoint in a subprocess — the thing the Makefile
    invokes — and asserts a clean exit, with src never naming the mock.
    """
    env = {**os.environ, "LIMPIADOR_LLM": "mock", "PYTHONPATH": str(_TESTS_DIR)}
    proc = subprocess.run(
        [sys.executable, "-m", "support", "run", "--repo", str(tmp_path), "--task", "tidy up"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

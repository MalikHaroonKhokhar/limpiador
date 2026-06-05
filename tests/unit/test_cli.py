"""Unit tests for the CLI surface (ARCHITECTURE.md §3 Layer 4, .clauderules §6).

The CLI is the only layer a user touches, so its contract is argument parsing and
exit codes — not agent behavior (that is the loop's, tested under integration).
These tests pin the parser: the ``run`` subcommand, the required ``--repo`` /
``--task``, and the ``--max-calls`` / ``--model`` overrides that feed named
config; and they pin that a missing required argument is a clear, non-zero exit
rather than a stack trace.
"""

from __future__ import annotations

import pytest

from limpiador.agent.guard import DEFAULT_CALL_CEILING
from limpiador.cli import build_parser, main


def test_parser_reads_the_run_subcommand_and_its_overrides() -> None:
    args = build_parser().parse_args(
        ["run", "--repo", "/tmp/r", "--task", "fix billing", "--max-calls", "7", "--model", "gpt-4o"]
    )
    assert args.command == "run"
    assert args.repo == "/tmp/r"
    assert args.task == "fix billing"
    assert args.max_calls == 7
    assert args.model == "gpt-4o"


def test_optional_flags_default_to_named_config() -> None:
    args = build_parser().parse_args(["run", "--repo", ".", "--task", "t"])
    assert args.max_calls == DEFAULT_CALL_CEILING  # the guard's named ceiling
    assert args.model is None  # no override → adapter's own default model


def test_missing_repo_is_a_clear_nonzero_exit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--task", "t"])
    assert exit_info.value.code != 0
    assert "--repo" in capsys.readouterr().err


def test_missing_task_is_a_clear_nonzero_exit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--repo", "."])
    assert exit_info.value.code != 0
    assert "--task" in capsys.readouterr().err


def test_no_subcommand_is_a_nonzero_exit() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code != 0


def test_max_calls_must_be_an_integer() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["run", "--repo", ".", "--task", "t", "--max-calls", "lots"])
    assert exit_info.value.code != 0

"""Command-line entrypoint (ARCHITECTURE.md §3, Layer 4).

Parses arguments, selects the run mode, boots the agent, and reports. This is
the only layer a user touches::

    limpiador run --repo . --task "fix the failing test in billing"

Argument parsing and run-mode selection live here; the orchestration they kick
off lives one layer down in :mod:`limpiador.agent`. The run mode is chosen by the
``LIMPIADOR_LLM`` env var through the adapter registration seam — the CLI never
names a concrete provider. ``--max-calls`` and ``--model`` feed named config (the
guard ceiling and the model override); the result of the run becomes the process
exit code: 0 on completion, non-zero on a guarded abort or an unrecoverable
configuration error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from limpiador.agent.guard import DEFAULT_CALL_CEILING, CallGuard
from limpiador.agent.llm import OPENAI_MODEL_ENV, LLMAdapter, build_adapter
from limpiador.agent.loop import RunResult, run
from limpiador.observability.errors import ConfigError
from limpiador.tools.registry import REGISTRY, ToolRegistry

# Process exit codes (CLEAN_CODE.md §7 — named, not bare integers): a clean
# completion, an unrecoverable startup/config error, and a guarded loop abort.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ABORTED = 2


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser: the ``run`` subcommand and its flags."""
    parser = argparse.ArgumentParser(
        prog="limpiador",
        description="Autonomous git maintenance agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="Run the agent against a repository.")
    run_cmd.add_argument("--repo", required=True, help="Path to the target repository.")
    run_cmd.add_argument(
        "--task", required=True, help="The maintenance task, in plain language."
    )
    run_cmd.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_CALL_CEILING,
        help=f"Hard ceiling on tool calls before the run aborts (default {DEFAULT_CALL_CEILING}).",
    )
    run_cmd.add_argument(
        "--model",
        default=None,
        help="Override the model the adapter uses (real mode only).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    adapter: LLMAdapter | None = None,
    registry: ToolRegistry | None = None,
) -> int:
    """Parse arguments and dispatch to the agent. Returns a process exit code.

    ``adapter`` and ``registry`` are injection seams for tests; in normal use
    they are ``None`` and resolved from the run mode and the default registry.
    Argument errors raise ``SystemExit`` (argparse's contract) with a non-zero
    code, so a missing ``--repo`` / ``--task`` is a clear message, not a crash.
    """
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run(args, adapter=adapter, registry=registry)
    return EXIT_ERROR  # unreachable: the subparser is required


def _run(
    args: argparse.Namespace,
    *,
    adapter: LLMAdapter | None,
    registry: ToolRegistry | None,
) -> int:
    """Boot the agent for the ``run`` subcommand and turn its result into a code."""
    repo = Path(args.repo)
    if not repo.is_dir():
        print(
            f"error: repository path does not exist or is not a directory: {repo}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    # --model feeds the adapter's named config through the documented env var, so
    # the override reaches the provider without the CLI importing it.
    if args.model:
        os.environ[OPENAI_MODEL_ENV] = args.model

    try:
        adapter = adapter if adapter is not None else build_adapter()
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR

    registry = registry if registry is not None else REGISTRY
    guard = CallGuard(ceiling=args.max_calls)
    result = run(
        args.task,
        registry=registry,
        adapter=adapter,
        guard=guard,
        system_prompt=_system_prompt(repo),
    )
    return _report(result)


def _system_prompt(repo: Path) -> str:
    """The standing instruction that anchors the agent to the target repository."""
    return (
        "You are limpiador, an autonomous git maintenance agent operating on the "
        f"repository at {repo}. Discover the tools you need, act, and call finish "
        "with a structured result when the task is complete."
    )


def _report(result: RunResult) -> int:
    """Print the run's outcome and map it to a process exit code."""
    if result.aborted:
        print(
            f"run aborted: hit the tool-call ceiling after {result.turns} turn(s) "
            "without finishing.",
            file=sys.stderr,
        )
        return EXIT_ABORTED
    print(result.result if result.result is not None else "")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())

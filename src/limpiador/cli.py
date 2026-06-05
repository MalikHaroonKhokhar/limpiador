"""Command-line entrypoint (ARCHITECTURE.md §3, Layer 4).

Parses arguments, selects the run mode, boots the agent, and reports. This is
the only layer a user touches::

    limpiador run --repo . --task "fix the failing test in billing"

Argument parsing and run-mode selection live here; the orchestration they kick
off lives one layer down in :mod:`limpiador.agent`.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser: the ``run`` subcommand and its flags."""
    parser = argparse.ArgumentParser(
        prog="limpiador",
        description="Autonomous git maintenance agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the agent against a repository.")
    run.add_argument("--repo", required=True, help="Path to the target repository.")
    run.add_argument(
        "--task", required=True, help="The maintenance task, in plain language."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the agent. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    raise NotImplementedError(
        f"limpiador {args.command!r} is not wired up yet — the agent loop lands "
        "in a later ticket (ARCHITECTURE.md §6)."
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())

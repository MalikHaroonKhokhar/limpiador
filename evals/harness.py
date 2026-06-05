"""Eval harness entrypoint (ARCHITECTURE.md §11.3).

Runs each eval case against a *fresh checkout* of its committed fixture (fixture
isolation is mandatory — one run's edits must not poison the next), drives the
real agent against the seeded defect, and asserts on both layers: outcome (goal
achieved) and trace (reasoned in the right order, under the call ceiling). At
least one case carries a planted red herring. Invoked by ``make eval`` as
``python -m evals.harness`` in REAL mode.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Run the eval suite and return a process exit code."""
    raise NotImplementedError(
        "The eval harness is not implemented yet — eval cases and the runner "
        "land in a later ticket (ARCHITECTURE.md §11.3)."
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())

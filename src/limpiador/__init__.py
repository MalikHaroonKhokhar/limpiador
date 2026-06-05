"""limpiador — an autonomous git maintenance agent.

Point it at a repository, give it a task in plain language ("fix the failing
test in billing", "rename ``calculate_total`` everywhere and don't break
anything"), and it investigates, plans, edits, verifies, and reports — driving
git, the GitHub API, the filesystem, the test runner, and a semantic (AST) view
of the code.

The package is layered (ARCHITECTURE.md §3-4) and the dependency direction is
strictly downward::

    cli  ->  agent  ->  tools  ->  observability / schemas

Nothing in a lower layer imports from a higher one, and nothing under ``tests/``
or ``evals/`` is imported by this package.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

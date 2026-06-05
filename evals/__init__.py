"""Eval harness — does the AGENT reason well? (ARCHITECTURE.md §11.3)

A separate concern from the test suite. The test suite (``tests/``) asks whether
the *code* works; evals ask whether the *agent reasons well*, against committed
git fixtures with a known seeded defect. Same real run mode as E2E, different
question — kept in a different top-level directory on purpose so a failure tells
you where to look. Nothing in this package is imported by ``src/limpiador``.
"""

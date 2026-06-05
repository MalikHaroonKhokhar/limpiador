"""``test.*`` / ``ci.*`` namespace — verification (ARCHITECTURE.md §5.3, 8 tools).

run_tests, run_subset, coverage, lint, typecheck, format, trigger_ci,
get_ci_status. ``run_tests`` emits a typed ``TestResult`` with structured
failures (``{test, file, line, message}``) that the agent consumes to locate and
fix the cause, then re-runs — the verification half of the fix loop (§8).
"""

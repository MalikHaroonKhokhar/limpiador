# evals/fixtures/ — Seeded fixtures (ARCHITECTURE.md §11.3)

Committed git repositories with a **known seeded defect** — a test that fails for
a reason you planted, a symbol used across three files, a PR diff with a
deliberate bug. Ground truth is known, so eval assertions are binary.

**Fixture isolation is mandatory:** every eval run operates on a fresh checkout,
never the committed original, with cleanup after.

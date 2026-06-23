# evals/fixtures/ — Seeded fixtures (ARCHITECTURE.md §11.3)

Committed git repositories with a **known seeded defect** — a test that fails for
a reason you planted, a symbol used across three files, a PR diff with a
deliberate bug. Ground truth is known, so eval assertions are binary.

**Fixture isolation is mandatory:** every eval run operates on a fresh checkout,
never the committed original, with cleanup after.

## The fixtures

Each directory is a small, deterministic repo with one planted defect, documented
in its own `README.md` (the ground truth that makes assertions binary):

| Fixture | Planted defect |
|---|---|
| `failing_test/`  | `calc.add` subtracts instead of adding; one test fails. |
| `rename_symbol/` | `compute` defined once, used across three files (`core`, `consumer`, `report`). |
| `bad_pr/`        | `pr.diff` flips `+` to `-` in `apply_restock` — a regression to reject. |
| `red_herring/`   | `pipeline.normalize` drops the first row; the recently-touched `settings.py` beside it is innocent. |

Each loads via `evals.harness.checkout_fixture(<name>)` as a fresh, materialised
git repo. The defects are exercised offline (no model) in
`tests/unit/test_eval_fixtures.py`.

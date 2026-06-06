# tests/reproduce/ — Reproduction tests (ARCHITECTURE.md §11.1)

One file per **observed behavior**, real model. A reproduction test captures
something the agent actually did during a real-model run that it should *not*
have done (or should have done and didn't), pins it as a failing test, and is
what the fix is verified against. It is the real-model analogue of a regression
test. Run with `make test-reproduce`.

## Convention

- **One file per behavior**, named for it: `test_<behavior>.py`. The module
  docstring states the observed behavior and **links the run trace** that
  surfaced it (a trace id / path under the run's observability output), so the
  reproduction is traceable to a real run.
- **Real model** (`LIMPIADOR_LLM=openai`): the behavior only appears with the
  real model — a mock just replays whatever we scripted. Marked
  `@pytest.mark.reproduce`.
- **Skips without a key**: each test is `skipif` on `OPENAI_API_KEY`, and
  `make test-reproduce` skips the whole tier cleanly when the key is unset, so
  CI stays green and no credit is spent.
- **Relaxed assertions**: assert the *behavior*, not an exact transcript. The
  real model is non-deterministic, so a reproduction asserts the property that
  matters (e.g. "references were resolved before the rename") — loose enough to
  pass on the model's natural variation, tight enough to catch the bug.
- **TDD red → green**: a reproduction test is committed 🔴 (it fails against the
  unfixed agent), then the fix is committed 🟢 (it passes). The two commits are
  the documentation of the fix.

Bound the cost: every reproduction run uses a low `CallGuard` ceiling and a tiny
seeded repo, so a real run is a handful of cheap turns.

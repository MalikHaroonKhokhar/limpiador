# Fixture: `red_herring` — the real cause beside a recently-touched distractor

A failing repo with two files: the one that is actually broken, and an innocent,
recently-refactored file that is the *obvious* suspect.

## Ground truth

- **The defect:** `pipeline.normalize` in `pipeline.py` slices `rows[1:]`, silently
  dropping the first row. It should iterate `rows`.
- **The symptom:** `test_pipeline.py::test_normalize_keeps_and_trims_every_row`
  fails — the first row is missing from the output.
- **The fix:** change `rows[1:]` to `rows` in `pipeline.py`. Nothing else.
- **The red herring:** `settings.py` is the most-recently-touched file (its
  docstring advertises a fresh refactor) and the natural first suspect, but it is
  correct and unrelated. A recency- or blame-led agent wastes a turn here; a
  well-reasoning one reproduces the failure and follows it to `pipeline.normalize`,
  leaving `settings.py` byte-for-byte unchanged.

Small and deterministic: one real bug, one tempting distractor.

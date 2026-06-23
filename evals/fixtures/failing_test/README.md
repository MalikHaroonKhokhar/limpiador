# Fixture: `failing_test` — one test fails for a planted reason

A repo with a single, deliberately broken function and an unrelated innocent file
beside it.

## Ground truth

- **The defect:** `calc.add` in `calc.py` computes `a - b` instead of `a + b`.
- **The symptom:** `test_calc.py::test_add` fails; `test_multiply` passes.
- **The fix:** change `return a - b` to `return a + b` in `calc.py`. Nothing else.
- **The red herring:** `formatter.py` is innocent and unrelated. A well-reasoning
  agent fixes `calc.add` and leaves `formatter.py` byte-for-byte unchanged; a
  distracted one edits it.

Small and deterministic: two functions, two tests, one-line cause.

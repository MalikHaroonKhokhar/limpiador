# Fixture: `rename_symbol` — one symbol, three known use sites

A package where a single function is defined once and used across three files, so
an incomplete rename is detectable.

## Ground truth

- **The symbol:** `compute`, defined exactly once, in `pkg/core.py`.
- **The known sites (3 files):**
  - `pkg/core.py` — defines `compute` and calls it inside `run()`;
  - `pkg/consumer.py` — calls `compute` in `total()`;
  - `pkg/report.py` — calls `compute` in `summary()`.
- **The task:** rename `compute` → `calculate` at every site.
- **The trap:** miss any one file and an import or call breaks. A correct rename
  resolves all references first, then renames the definition and all three sites.

Small and deterministic: one definition, three call sites, no ambiguity.

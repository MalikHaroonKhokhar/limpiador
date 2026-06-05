# tests/reproduce/ — Reproduction tests (ARCHITECTURE.md §11.1)

One test per reported behavior, real model. Starts 🔴 failing (TDD red), becomes
🟢 passing after the fix. Uses the real model to match actual observed behavior
rather than a seeded fixture. Run with `make test-reproduce`.

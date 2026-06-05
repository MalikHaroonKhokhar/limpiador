# tests/integration/ — Layers 2 & 3 (ARCHITECTURE.md §11.1)

- **`*_mock.py` — Layer 2 (mock model, real temp git repos).** Drive the full
  loop with the deterministic mock LLM over scripted tool-call sequences; assert
  the loop reaches the expected end state and the reviewer subagent runs
  isolated with its scope enforced. Run with `make test-integration`.

- **`*_e2e.py` — Layer 3 (REAL OpenAI + REAL git/github).** Tagged
  `@pytest.mark.e2e`. Invoke limpiador the way a user does — through the CLI —
  and test the **plumbing**. Runs only against the throwaway sandbox repo and
  skips gracefully when `OPENAI_API_KEY` / `GITHUB_TOKEN` /
  `LIMPIADOR_SANDBOX_REPO` are unset. Run with `make test-e2e`. Never in the
  default `make test`.

# limpiador 🧹

An autonomous **git maintenance agent**. Point it at a repository, give it a
task in plain language — _"fix the failing test in the billing module"_,
_"rename `calculate_total` everywhere and don't break anything"_, _"triage issue
#42 and open a PR"_ — and it investigates, plans, edits, verifies, and reports,
driving real `git`, the GitHub API, the filesystem, the test runner, and a
semantic (AST) understanding of the code.

The runtime model is **OpenAI** (function calling). The interface is a **CLI**.

> **Status: skeleton.** This is the bootstrap commit — the layered package, the
> test harness, and the build tooling are in place; the loop, the tools, and the
> registry land in subsequent tickets. Run `make help` for the command surface.

## Quickstart

```bash
make setup        # create venv, install ".[dev]", copy .env.example -> .env
make test         # default suite: unit + mock-integration (mock model, free, no network)
make help         # full command reference
```

Then edit `.env` with your `OPENAI_API_KEY` (and, for E2E, `GITHUB_TOKEN` +
`LIMPIADOR_SANDBOX_REPO`) before running anything in real mode.

## Running the agent

```bash
make dev-mock REPO=. TASK="fix the failing test"   # mock model — free, deterministic
make run      REPO=. TASK="fix the failing test"   # REAL OpenAI — costs credits
```

## Two run modes, not two servers

limpiador is a CLI, so the "two environments" discipline is expressed through
**run mode**, selected by `LIMPIADOR_LLM` and the Makefile targets:

| Mode   | `LIMPIADOR_LLM` | Behaviour                                  | Used for                                   |
|--------|-----------------|--------------------------------------------|--------------------------------------------|
| mock   | `mock`          | deterministic mock LLM, temp git, no network | unit + integration tests, CI, dev, free   |
| real   | `openai`        | real OpenAI + real git/github (costs credits) | eval harness, reproductions, the demo     |

Reasoning behavior can only be validated in **real** mode; mechanics (routing,
dispatch, compaction, retry) are validated in **mock** mode. Using the wrong
mode wastes either credits or confidence.

## Testing at a glance (ARCHITECTURE.md §11)

| Command               | Tier                        | Mode | Costs $ |
|-----------------------|-----------------------------|------|---------|
| `make test`           | unit + mock-integration     | mock | no      |
| `make test-unit`      | Layer 1: pure logic         | mock | no      |
| `make test-integration` | Layer 2: full loop, temp repos | mock | no  |
| `make test-e2e`       | Layer 3: plumbing, real APIs | real | yes ⚠️ |
| `make eval`           | agent reasoning on fixtures | real | yes ⚠️ |

The real-mode tiers skip gracefully when `OPENAI_API_KEY` / `GITHUB_TOKEN` /
`LIMPIADOR_SANDBOX_REPO` are unset, so CI stays green and a real repo is never
touched.

## Layout

```
src/limpiador/      the layered package (cli → agent → tools → observability/schemas)
  agent/            the loop, context strategy, call ceiling, LLM adapter
  tools/            registry + the five tool namespaces (git/github/fs/ast/test)
  subagents/        the isolated, scoped reviewer subagent
  observability/    tracing, typed errors, retry + rate limiting
  schemas.py        typed pydantic I/O contracts shared across tools
tests/              does the CODE work? (unit / integration / reproduce)
evals/              does the AGENT reason well? (harness + seeded fixtures)
```

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the full system design (the map).
- **[.clauderules](.clauderules)** — agent-development guardrails + run modes.
- **[CLEAN_CODE.md](CLEAN_CODE.md)** — the code-style contract.
- **[MEMO.md](MEMO.md)** — the one-page brief deliverable.

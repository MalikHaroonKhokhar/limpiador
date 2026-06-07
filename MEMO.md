# limpiador — MEMO

**limpiador** is an autonomous git-maintenance agent: point it at a repository,
give it a task in plain language, and it investigates → plans → edits → verifies
→ reports over a real OpenAI function-calling loop. It ships as a CLI; the same
engine drives a deterministic mock model for tests and a real model for live work.

## What I built

A production-shaped agent that holds the brief's five properties, each mapped to a
named, greppable part of the tree (ARCHITECTURE.md §1):

- **#1 — 57 tools across 5 namespaces** (`git`, `github`, `fs`, `ast`, `test`/`ci`)
  with model-driven selection. The model never sees 57 schemas: it sees three
  meta-tools and *discovers* the rest through a searchable registry
  (`tools/registry.py`). This is the property the design goes deepest on.
- **#2 — Subagent orchestration.** A reviewer runs in an isolated context with a
  read-only, scoped registry and returns a typed `ReviewResult`
  (`subagents/reviewer.py`) — it cannot even *load* a write tool.
- **#3 — Long-horizon execution.** The loop sustains 20+ tool calls with explicit
  context compaction/eviction and a call-count guard that aborts runaway loops
  (`agent/loop.py`, `agent/context.py`).
- **#4 — Production scaffolding.** Typed errors, bounded retries, rate limiting, a
  structured tracer, an eval harness, and a 500-plus-test suite across unit,
  mock-integration, real-mode E2E, and reproduction tiers (`observability/`,
  `evals/`, `tests/`).
- **#5 — Composable tool I/O.** Tools exchange typed pydantic objects, so one
  tool consumes another's structured output (e.g. `github.get_pr` → reviewer)
  with no string-parsing seam (`schemas.py`).

## The decision I'd defend

**Dynamic tool loading via a searchable registry, over a static fifty-tool
registry handed to the model on every turn** (ARCHITECTURE.md §15).

Register everything, pass it all, let the model pick — that is what most reach for
first, and it works at five tools. It loses at fifty for three reasons that
compound:

- **Context economics.** Fifty schemas is thousands of tokens replayed on every
  one of 20+ turns. Dynamic loading sends three meta-tool schemas plus only the
  handful actually loaded, so cost scales with what a task *uses*, not with what
  *exists*.
- **Selection quality.** A fifty-item menu degrades the model's choice; a short,
  search-ranked candidate list per need keeps selection sharp.
- **Subagent scoping.** The read-only reviewer's tool set falls out of the same
  registry for free; the static design needs a *second* mechanism to express it.

One decision buys property #1, property #2's isolation, and the cost profile at
once — load-bearing, not cosmetic. Its honest cost is ARCH_DEBT_001 below.

## What I cut, and why (ARCHITECTURE.md §14)

- **No dollar-level budget accounting** — a call-count kill-switch bounds runaway
  loops and token totals live in the tracer; per-dollar tracking adds nothing for
  a five-day, mock-developed build.
- **Single-language AST** — the `ast.*` tools parse the primary target for real;
  more languages are the same tree-sitter approach extended, a bounded follow-on.
- **No GitHub App / webhook deployment** — ships as a CLI; the App wrapper that
  reacts to issues and PRs is the natural productization, explicitly later.
- **No persistent run store** — traces are per-run; aggregating them into a
  queryable history is out of scope.

Deliberate, not accidental: go deep on the registry, context strategy, subagent
isolation, and evals; leave the productization surface for later.

## What another week would address

- **ARCH_DEBT_001 — semantic tool ranking** *(the first thing I'd do)*.
  `search_tools` ranks by keyword overlap, so a query phrased unlike a tool's
  description can rank the right tool low and burn a turn. Fix: embed descriptions
  once at startup and rank by cosine similarity — no per-turn cost, interface
  unchanged. This is the direct cost of the defended decision above.
- **ARCH_DEBT_002 — lossy compaction on wide refactors.** Summarizing early reads
  can drop a detail needed much later (an import edge noted on call 4, needed on
  call 22), forcing a re-read. Fix: promote import/dependency edges into durable
  state at read time so they survive eviction.
- **Polyglot AST** — extend the `ast.*` namespace beyond the primary language.

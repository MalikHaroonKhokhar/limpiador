# limpiador — MEMO (the one-page deliverable)

> **Status: skeleton.** This is the structure for the one-page brief deliverable;
> sections fill in as the build progresses. Keep it to one page — it is a memo,
> not a second architecture document. The full design lives in `ARCHITECTURE.md`.

## What limpiador is

One or two sentences. An autonomous git maintenance agent: point it at a repo,
give it a task in plain language, it investigates → plans → edits → verifies →
reports over a real OpenAI function-calling loop.

## The decision I'd defend

_The single design choice this submission stakes its reputation on
(ARCHITECTURE.md §15)._

**Dynamic tool loading via a searchable registry, over a static fifty-tool
registry handed to the model every turn.** Why the obvious alternative loses at
fifty tools: schema-token cost replayed across 20+ turns, selection quality
degrading as the menu grows, and no way to express scoped subagent tool sets
without a second mechanism. One decision that buys property #1, property #2's
scoping, and the cost profile at once. _(Expand with the measured numbers once
the registry and a real run exist.)_

## What I cut, and why (ARCHITECTURE.md §14)

- **No dollar-level budget accounting** — a call-count kill-switch bounds runaway
  loops; token accounting in the tracer covers the observability need.
- **No multi-language AST** beyond the primary target — same tree-sitter
  approach extends later; bounded follow-on, not a five-day item.
- **No GitHub App / webhook deployment** — ships as a CLI; the App wrapper is the
  natural productization and explicit future work.
- **No persistent run store** — traces are per-run.

## How it's tested (ARCHITECTURE.md §11)

Two distinct concerns kept apart: the **test suite** (does the code work? — unit
+ mock-integration on a deterministic mock model, plus a tiny real-mode E2E tier
for the plumbing) and the **eval harness** (does the agent reason well? — real
model against seeded fixtures with known ground truth, including a planted red
herring). Same real run mode for the last two; different question, different
directory.

## What I'd do next

_The honest "given another week" list — the first item is usually the proper fix
for the highest-frequency entry in the architectural debt tracker (.clauderules
§8)._

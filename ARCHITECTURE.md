# limpiador — Architecture

A production-shaped autonomous **git maintenance agent**. You point it at a
repository and give it a task in plain language — "fix the failing test in the
billing module", "rename `calculate_total` everywhere and don't break anything",
"triage issue #42 and open a PR" — and it investigates, plans, edits, verifies,
and reports, driving real `git`, the GitHub API, the filesystem, the test runner,
and a semantic (AST) understanding of the code.

The runtime model is **OpenAI** (function calling). The development environment
is **Claude Code**. The interface is a **CLI**. Everything else below is the
design that makes those choices hold up at production grade.

This document explains the architecture in full and **contains no code by
design** — it is the map, not the territory. Code lives in `src/`, contracts
live in the type definitions, and the rules that govern how the code is written
live in `.clauderules` and `CLEAN_CODE.md`.

---

## 1. The five properties, and where each one lives

The brief names five properties that must hold across the build. This is the
single most important section of the document: it is the contract between the
brief and the code. Every property maps to a named, locatable part of the
system rather than to a vibe.

| # | Property | Where it lives in limpiador |
|---|----------|------------------------------|
| 1 | 50+ tools, 4+ namespaces, model-driven selection, coherent at scale | `tools/` (5 namespaces, 57 tools) + `tools/registry.py` (dynamic loading via `search_tools`) |
| 2 | Subagent orchestration in an isolated context with a scoped tool set | `subagents/reviewer.py` (`spawn_reviewer`, read-only scoped registry, typed `ReviewResult`) |
| 3 | Long-horizon execution (20+ calls) with explicit context management | `agent/loop.py` + `agent/context.py` (compaction / eviction expressed in code) |
| 4 | Production scaffolding (observability, retries, rate limiting, typed errors, eval harness, test suite) | `observability/`, `evals/`, `tests/` |
| 5 | Composable tool I/O — one tool consumes another's structured output | `schemas.py` (typed pydantic contracts) + the tool chains in §9 |

If a reviewer reads only this table and then greps the tree for the named
files, the claim should hold. That is the intent.

---

## 2. Design philosophy

Three convictions shape every decision that follows.

**The registry is the hard part, not the tools.** Writing fifty tools is
mechanical. Keeping fifty tools *coherent* — so the model selects the right one
rather than a human routing it through fifty conditionals — is the real
engineering, and it is where most submissions collapse. limpiador treats tool
selection as a first-class subsystem (§5), not as a dispatch table.

**The cheapest correct design and the highest-scoring design are the same
design.** Small typed tool outputs, dynamic tool loading, and context
compaction each independently reduce token cost *and* satisfy a brief property.
We never trade one against the other because they do not conflict.

**The agent is tested like software, not demoed like a toy.** There are two
distinct testing concerns — does the *code* work (test suite) and does the
*agent reason well* (eval harness) — and they are kept physically and
conceptually separate (§11). A passing eval on a seeded fixture with known
ground truth is the difference between engineering and a vibe check.

---

## 3. System overview

limpiador is a single Python process invoked from the command line. It has four
conceptual layers, and the dependency direction is strictly downward — upper
layers depend on lower layers, never the reverse.

```
                 ┌─────────────────────────────┐
   Layer 4       │            CLI               │   entrypoint, arg parsing,
   (interface)   │         cli.py               │   run-mode selection
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────▼──────────────┐
   Layer 3       │        AGENT CORE           │   the loop, context strategy,
   (orchestration)│  loop · context · llm      │   the call ceiling, subagents
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────▼──────────────┐
   Layer 2       │       TOOL SUBSYSTEM        │   registry, dynamic loading,
   (capability)  │  registry · base · tools/* │   the 5 namespaces
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────▼──────────────┐
   Layer 1       │   FOUNDATIONS / PLUMBING    │   typed schemas, observability,
   (infrastructure)│ schemas · observability   │   retry, errors, rate limiting
                 └─────────────────────────────┘
```

The architectural integrity rule is simple and enforced: nothing in a lower
layer may import from a higher one, and nothing in `tests/` or `evals/` may be
imported by `src/`. The mock model adapter is the one piece that looks like it
might cross this line — it does not. The mock lives in the test layer and is
*injected into* the agent core through the same interface the real OpenAI
adapter implements (§10). Production code never knows whether it is talking to a
real model or a fake one.

---

## 4. Directory structure

```
limpiador/
  pyproject.toml
  Makefile                      # run-mode targets, test targets (see §12)
  MEMO.md                       # the one-page brief deliverable
  README.md
  ARCHITECTURE.md               # this file
  .clauderules                  # agent-development guardrails
  CLEAN_CODE.md                 # code-style contract

  src/limpiador/
    __init__.py
    cli.py                      # entrypoint: limpiador run --repo . --task "..."

    agent/
      loop.py                   # the orchestration spine
      context.py                # working memory + compaction/eviction (property 3)
      llm.py                    # OpenAI adapter — the ONLY provider-specific file
      guard.py                  # call-count kill-switch (loop-safety, not budget)

    tools/
      base.py                   # Tool ABC, namespace + schema conventions
      registry.py               # dynamic loading + search_tools (property 1)
      git_tools.py              # git.*    namespace
      github_tools.py           # github.* namespace
      fs_tools.py               # fs.*     namespace
      ast_tools.py              # ast.*    namespace (the differentiator)
      test_tools.py             # test.* / ci.* namespace

    subagents/
      reviewer.py               # spawn_reviewer: isolated, scoped (property 2)

    observability/
      tracing.py                # structured per-call traces, token accounting
      errors.py                 # ToolError hierarchy (typed errors)
      retry.py                  # exponential backoff + token-bucket rate limiter

    schemas.py                  # pydantic types for all tool I/O (property 5)

  tests/                        # does the CODE work?  (see §11)
    unit/                       # Layer 1: pure logic, mock model, no network
    integration/
      *_mock.py                 # Layer 2: full loop, mock model, temp git repos
      *_e2e.py                  # Layer 3: REAL openai + REAL git/github (throwaway repo)
    reproduce/                  # one test per reported behavior (real model)
    conftest.py                 # temp git repos, mock LLM, loaded registry

  evals/                        # does the AGENT reason well?  (see §11)
    harness.py
    cases/
    fixtures/                   # committed git repos with KNOWN seeded defects
    report.py
```

---

## 5. The tool subsystem — property #1 in depth

### 5.1 The problem the registry solves

A naive agent sends every tool definition to the model on every turn. With
fifty tools that is roughly 8,000–15,000 tokens of schema replayed on all
twenty-plus calls of a long task — the cost is enormous and, worse, the model's
selection quality degrades as the menu grows. Fifty tools presented at once is
how a registry "collapses into a chain of fifty conditional dispatches" in
spirit even if not in syntax: the model is overwhelmed and the human ends up
hard-routing to compensate.

### 5.2 Dynamic tool loading

limpiador never shows the model fifty schemas. The model always sees a small,
fixed **core**:

- `search_tools(query)` — returns ranked one-line tool summaries (name +
  description + namespace), not full schemas.
- `load_tool(name)` — promotes a tool's full schema into the active set for
  subsequent turns.
- `finish(result)` — signals task completion with a structured result.

Everything else is discovered. The model reasons about what it needs ("I need
to find where this symbol is used"), searches the registry, and loads the tools
the task actually requires. This is the architecture that *proves*
model-driven selection: the model cannot fall back on a tool it was handed,
because it was handed almost nothing. It must choose.

The registry holds all fifty-seven tools registered at import time, tracks which
are currently loaded into context, and exposes only `core + loaded` schemas to
the LLM adapter each turn. Search ranking is a local, deterministic operation
over tool names and descriptions — no model call, no cost. (The current
ranking strategy and its known limitation are recorded in the architectural
debt tracker in `.clauderules`.)

### 5.3 The five namespaces (57 tools)

Tools are grouped into coherent namespaces. Nothing here is filler invented to
reach a number; each namespace is a genuine capability surface a real
maintenance agent needs.

**`git.*` — local repository state + publishing (13)**
`status`, `diff`, `log`, `show`, `branch_list`, `branch_create`, `checkout`,
`stage`, `commit`, `reset`, `stash`, `push`, `blame`

**`github.*` — remote collaboration (14)**
`get_issue`, `list_issues`, `create_issue`, `comment_issue`, `get_pr`,
`list_prs`, `create_pr`, `review_pr`, `request_changes`, `merge_pr`,
`list_checks`, `get_check_logs`, `get_file_at_ref`, `search_code`

**`fs.* — filesystem (10)`**
`read_file`, `write_file`, `list_dir`, `glob`, `grep`, `move`, `delete`,
`mkdir`, `file_stat`, `apply_patch`

**`ast.* — semantic code understanding (12)** — the differentiator**
`parse_file`, `list_symbols`, `find_definition`, `find_references`,
`call_graph`, `dependency_tree`, `find_dead_code`, `detect_cycles`,
`rename_symbol`, `extract_function`, `list_imports`, `complexity_score`

**`test.* / ci.* — verification (8)`**
`run_tests`, `run_subset`, `coverage`, `lint`, `typecheck`, `format`,
`trigger_ci`, `get_ci_status`

The `ast.*` namespace is where limpiador goes deeper than a typical coding
agent. Most agents read files and shell out to grep; limpiador resolves symbols,
traverses call graphs, and performs safe cross-file renames using real parsing.
This is the depth a reviewer respects, and it is load-bearing for composability
(§9) and for the "decision I'd defend" in the MEMO.

### 5.4 Why namespaces matter beyond grouping

Namespaces are not cosmetic. They are how scoped tool sets are expressed (the
reviewer subagent gets `fs.*` read-only + `ast.*` + `github.get_pr`, and
nothing that writes — §7), and they are how `search_tools` ranking stays
legible. A flat bag of fifty tools cannot be scoped or reasoned about; five
named surfaces can.

---

## 6. The agent loop — the spine

`agent/loop.py` is the smallest file that matters most. One turn of the loop:

1. **Guard check.** Before anything, the call-count kill-switch (`guard.py`)
   verifies the run has not exceeded its hard ceiling of tool calls. If it has,
   the run terminates with a typed `RunAborted` result. This is loop-safety, not
   budget accounting — a long-horizon agent that loses coherence fails by
   looping, and the ceiling is how that failure is bounded.
2. **Assemble context.** Pull the current message history plus the active tool
   schemas (`core + loaded`) from the registry.
3. **Model call.** Send to the OpenAI adapter. Record the response — tokens,
   latency, and the tool calls requested — in the tracer.
4. **Dispatch.** For each tool call returned (OpenAI may return several in one
   turn), execute it. The result is always a typed object (§8). Tool failures
   raise typed `ToolError`s, which are caught and folded back into context as
   structured error results so the model can recover rather than crash.
5. **Fold and compact.** Add results to the context. If the estimated token
   footprint crosses the compaction threshold, run the eviction strategy (§7).
6. **Terminate or repeat.** If the model called `finish`, return its structured
   result. Otherwise loop.

The loop does only orchestration. It does not know what any individual tool
does, does not parse free text, and does not branch on tool identity. That
ignorance is deliberate: it is what keeps the system from degenerating into the
fifty-conditional anti-pattern the brief warns against.

---

## 7. Context management — property #3 in depth

A long-horizon task spans twenty-plus tool calls. If every raw tool result —
full file contents, full diffs, full check logs — accumulated in the context
window, two things would happen: cost would grow quadratically (each turn
replays all prior turns) and the model would lose the plan under the weight of
stale detail. limpiador's context strategy is **explicit, in code, in
`context.py`** — not an implicit consequence of the model's window size.

The strategy has three parts:

**Working memory, not transcript.** The context object distinguishes durable
state (the task, the plan, resolved sub-goals, key findings) from transient
payloads (the full text a tool returned). Durable state always survives;
transient payloads are candidates for eviction.

**Summarize-then-evict.** When a raw payload is no longer needed for the
immediate next step, it is replaced by a compact structured summary. A file
that was read becomes "read `billing.py`: defines `calculate_total` (line 40),
imported by `checkout.py`, `report.py`" rather than two hundred lines of source.
The eviction logic is written out explicitly so a reviewer can read *how*
limpiador decides what to drop, which is precisely what the brief asks for.

**Threshold-triggered, not per-call.** Compaction runs when the estimated
footprint crosses a threshold, not on every turn — compacting too eagerly
throws away context the very next step needs; compacting too late blows the
window. The threshold and the eviction policy are configuration, visible and
tunable.

**The plan is written by the agent, in protocol verbs.** Durable state is only
meaningful if something fills it, so the fixed core carries two verbs alongside
`finish`: `plan_add(sub_goals)` commits the steps the agent intends to work
through, and `plan_resolve(sub_goal)` marks one done. Like `finish`, they act on
the *run*, not the repo — the registry validates them and the loop applies them
to the context. Because they land in durable state, compaction never touches
them: the agent can lose every raw file it read and still know what it planned,
what it finished, and what remains.

The result is that limpiador can run a twenty-plus-call investigation while the
context stays roughly flat in size, because raw detail is continuously
distilled into structured findings. The plan stays coherent because the plan
lives in durable state that is never evicted.

Every compaction pass that evicts anything emits the `[CONTEXT COMPACTION]`
trace tag recording how many payloads it dropped and the footprint before and
after, so the property is *observable in a run's trace* rather than merely
claimed. A captured real session — 34 tool calls, 10 compactions, a 7-step plan
carried to completion without re-litigating a resolved step — is committed at
`traces/har-33/long-session.md`, and the path is guarded by
`tests/reproduce/test_long_session_stays_coherent.py` (real model) and
`tests/integration/test_long_session_coherence.py` (deterministic mock).

---

## 8. Composable tool I/O — property #5 in depth

Every tool consumes and emits **typed pydantic objects** defined in
`schemas.py`, never free text. This is the contract that lets tools chain.

Because outputs are typed, the output of one tool can be the input of another
without a parsing step in between. The composition is real — a structured
object handed across — not a string the next tool re-parses.

The canonical chains limpiador relies on:

- `ast.find_references` → `ast.rename_symbol`. The references result (a typed
  list of `{file, line, symbol}` locations) is consumed directly by the rename
  tool, which edits exactly those sites. Renaming without first consuming
  references is how agents miss call sites and break builds.
- `test.run_tests` → the fix loop. A failed test run emits a typed
  `TestResult` with structured failures (`{test, file, line, message}`), which
  the agent uses to locate and fix the cause, then re-runs.
- `github.get_pr` (diff) → `spawn_reviewer` → `report`. The PR diff feeds the
  reviewer subagent, whose typed `ReviewResult` feeds the comment/report tools.

The typing also pays off in testing: a unit test can assert that one tool's
output object validates as another tool's input object, proving the
composability contract holds without running the agent at all.

---

## 9. Subagent orchestration — property #2 in depth

The brief is explicit that a function call relabelled as a subagent does not
count. limpiador's subagent is genuinely isolated on three axes.

**Isolated context.** `spawn_reviewer` does not pass the parent's message
history to the subagent. The reviewer starts with a fresh context containing
only its task and the structured inputs it was given (the PR diff, the changed
files). The parent's reasoning, tool history, and accumulated state are invisible
to it. This is contamination isolation — the reviewer's judgment is not biased by
the parent's hypotheses.

**Scoped tool set.** The reviewer is constructed with a *different registry* —
a read-only scope containing `fs.*` reads, the `ast.*` namespace, and
`github.get_pr`, and deliberately excluding everything that writes: no
`fs.write_file`, no `git.commit`, no `github.merge_pr`. The scoping is enforced
at construction, visible in code, and structurally different from the parent's
full registry. A reviewer that could commit would not be a reviewer.

**Structured return.** The reviewer runs its own loop to completion and returns
a single typed `ReviewResult` to the parent — a list of findings, each with
severity, file, line, and a suggested change, plus an overall verdict. The
parent receives that object and nothing else; the subagent's internal tool
calls and reasoning do not leak back. The handoff is a structured result across
an isolation boundary, which is the definition the brief asks for.

The reviewer is also the most natural place for the agent to demonstrate
judgment under the red-herring eval case (§11): a reviewer with isolated context
and scoped tools that still catches a planted regression is real evidence of
the architecture working.

---

## 10. The LLM adapter and model routing

All provider-specific logic is quarantined in `agent/llm.py` behind a single
interface: take messages and active tool schemas, return a response with text
and tool calls. Two implementations satisfy that interface — the real OpenAI
adapter and the deterministic mock used in tests (§11). Because the agent core
depends only on the interface, the provider is swappable and the mock is
injectable without the agent ever knowing which it holds.

**Model routing.** Not every turn needs the strongest model. Tool-selection and
routine dispatch turns use a cheaper, faster model; planning and synthesis turns
escalate to a stronger one. The routing policy lives in the adapter, and the
specific model names and per-token prices are treated as configuration to be
verified against current OpenAI pricing rather than hard-coded against
assumptions, because that lineup moves. The *structure* — cheap by default,
escalate rarely — is fixed; the *names* are config.

**Prompt caching.** The system prompt and the core tool schemas form a stable
prefix kept byte-identical across turns so the provider's prompt cache can serve
them cheaply on repeat. On a twenty-call task with a fixed prefix this is the
single largest cost lever, and it is free to adopt — it only requires not
mutating the prefix.

---

## 11. Testing strategy — property #4's two concerns

limpiador holds two distinct testing concerns apart, because conflating them is
the usual failure. A unit test failing means the *code* is broken. An eval
failing means the *agent reasoned poorly*. They use different tools, assert
different things, and a reviewer looks for the seam between them.

### 11.1 The test suite (`tests/`) — does the code work?

Three tiers, cheapest first. The first two run against the **mock model**
(deterministic, free); the third runs in **real mode** against a **throwaway
repo** because it is testing the live pipeline, not the agent's reasoning.

- **Layer 1 — Unit (`tests/unit/`)** — pure logic, no model, no network.
  Registry ranking and loading, each tool's behavior given known repo state,
  context compaction evicting raw payloads while preserving plan state,
  retry/backoff firing on transient errors, and the schema round-trip that
  proves one tool's output validates as another's input. Dozens of tests,
  sub-second.
- **Layer 2 — Mock integration (`tests/integration/*_mock.py`)** — the full
  loop on the mock model: scripted tool-call sequences drive the loop to an
  expected end state; the reviewer subagent runs isolated with its scope
  enforced and returns a typed result. Real temp git repos (created in
  `conftest.py`), mock LLM, real tool code. A handful of tests, seconds.
- **Layer 3 — E2E (`tests/integration/*_e2e.py`)** — REAL OpenAI, REAL git, and
  the REAL GitHub API, invoked the way a user invokes it (through the CLI, not
  by calling the loop in-process). This tier tests the *plumbing*: does the CLI
  parse args, boot the agent, authenticate, write a real commit, open a real PR,
  and exit cleanly. It runs only against a dedicated **throwaway sandbox repo**
  and **skips gracefully** when the sandbox, token, or key is not configured, so
  it never touches a real repo and never breaks CI. Relaxed assertions (the
  model is non-deterministic). Kept deliberately tiny — 2–4 tests — because it
  costs credits.

The mock model is not a shortcut for the first two tiers — it is the test
infrastructure. The same adapter that lets you develop at zero cost is what
makes the unit and integration suites deterministic and flake-free.

A reproduction tier (`tests/reproduce/`) holds one real-model test per reported
behavior, starting red and going green once fixed — the same TDD discipline as
the rest, matched to actual observed behavior rather than a fixture.

### 11.2 E2E versus evals — the distinction that matters

Layer 3 E2E and the eval harness both run in real mode and both cost credits,
so it is tempting to merge them. Do not. They answer different questions:

- **E2E** asks *"does the whole pipeline work end to end against real
  systems?"* — the **plumbing** is under test. An E2E test fails when auth
  breaks, the CLI mis-parses, or a commit doesn't land, even if the agent would
  have reasoned perfectly.
- **Evals** ask *"does the agent reason well?"* — the **reasoning** is under
  test, against a seeded fixture with known ground truth. An eval fails when the
  agent investigates badly, even if every pipe worked.

They live in different directories for this reason — E2E in `tests/` because it
tests your code's integration, evals in `evals/` because they test the model's
judgment. Same run mode, different concern. Keeping them apart is what lets a
failure tell you where to look.

### 11.3 The eval harness (`evals/`) — does the agent reason well?

Runs against the **real OpenAI model**, because the point is to test reasoning,
which a mock cannot exercise. This is where the handful of real-credit runs go.

Each eval case is a **committed git repo fixture with a known seeded defect** —
a test that fails for a reason you planted, a symbol used across three files, a
PR diff with a deliberate bug. Because ground truth is known, assertions are
binary, which converts fuzzy "did it maintain the repo well" into pass/fail.

Every case asserts on two layers:

- **Outcome** — did it achieve the goal? The failing test now passes; the
  rename touched all sites and broke nothing; the planted regression was
  flagged.
- **Trace** — did it reason correctly rather than luckily? It called
  `ast.find_references` *before* renaming; it called tools in a sensible order;
  it stayed under the call ceiling. Trace assertions catch the agent that
  reached the right answer by the wrong path — the failure that bites in
  production.

**Fixture isolation is mandatory.** Every eval run operates on a fresh checkout
of the fixture, never the committed original, with cleanup after. Without this,
one run's edits poison the next and you chase phantoms.

**At least one case carries a planted red herring** — an innocent
recently-changed file sitting next to the actually-broken one. An agent that
pattern-matches "most recent change = culprit" fails it; one that gathers
evidence passes. That single case is the strongest evidence of real
investigation depth, and it is a concrete MEMO talking point.

---

## 12. Execution model and run modes

limpiador is a CLI, not a server, so the "two environments" discipline is
expressed through **run mode** (an env var plus a Makefile target) rather than
through ports.

- **Mock mode** — mock LLM adapter, temp git fixtures. Fast, deterministic,
  free. The mode for unit and integration tests, local development, and CI. The
  entire architecture can be built and proven in this mode before a single real
  credit is spent.
- **Real mode** — real OpenAI, real git repositories. Slow, non-deterministic,
  costs credits. The mode for the eval harness, for reproducing a specific
  reported behavior, and for the demo recording.

The rule (carried into `.clauderules`): reasoning behavior can only be validated
in real mode, because mock mode by definition does not exercise the model's
judgment. Mechanics — routing, dispatch, format, compaction — are validated in
mock mode because they are deterministic. Using the wrong mode for the wrong
question wastes either credits or confidence.

---

## 13. Observability, resilience, and typed errors

The `observability/` layer is the production scaffolding the brief enumerates,
and it is built from the start rather than bolted on.

**Tracing (`tracing.py`).** Every tool call and every model call is recorded
structurally — which tool, with what input, returning what, how long it took,
how many tokens. The trace is what the eval harness asserts against (§11) and
what the demo surfaces. Token accounting lives here; even with dollar-budgeting
out of scope, knowing the token cost of a run is basic observability.

**Retries and backoff (`retry.py`).** External calls — GitHub and the model —
fail transiently. They are wrapped in exponential backoff with a bounded retry
count and typed give-up behavior. A retry that never gives up is just a slower
infinite loop.

**Rate limiting (`retry.py`).** A token-bucket limiter caps the rate of
external calls so limpiador does not get throttled or banned by GitHub during a
busy run. Because limpiador hits real external APIs, this is load-bearing, not
theater.

**Typed errors (`errors.py`).** A `ToolError` hierarchy distinguishes failure
kinds — not-found, permission, transient, malformed-input — so the agent loop
can fold a structured, recoverable error back into context instead of crashing.
The model can read "file not found" and adapt; it cannot adapt to a stack trace.

---

## 14. What is deliberately out of scope

Stated plainly so the MEMO's "what I cut" writes itself, and so no reviewer
mistakes a deliberate omission for an oversight.

- **No dollar-level budget accounting.** A call-count kill-switch bounds runaway
  loops (§6); per-dollar tracking is intentionally cut because mock-mode
  development makes it unnecessary for the five-day build, and token accounting
  in the tracer covers the observability need.
- **No multi-language AST beyond the chosen target.** The `ast.*` namespace is
  built on real parsing for the project's primary language; extending the same
  tree-sitter approach to further languages is a known, bounded follow-on, not a
  five-day item.
- **No GitHub App / webhook deployment.** limpiador ships as a CLI you point at
  a repo. The CLI core is the reusable engine; an App wrapper that reacts to
  issues and PRs is the natural productization and is explicitly future work.
- **No persistent run store.** Traces are emitted per run; aggregating them into
  a queryable history is out of scope for the build.

These cuts are coherent with the brief's framing — "scoped beyond five days,
depth not completion." limpiador chooses to go deep on the registry, the
context strategy, the subagent isolation, and the eval harness, and to leave the
productization surface for later.

---

## 15. The defensible design decision (MEMO seed)

The decision limpiador would defend against a reasonable alternative:
**dynamic tool loading via a searchable registry, over a static fifty-tool
registry handed to the model on every turn.**

The alternative is what many engineers reach for first — register all the tools,
pass them all, let the model pick. It is simpler and it works at five tools.
limpiador rejects it at fifty because: the static approach replays thousands of
schema tokens on every one of twenty-plus turns (cost), it degrades selection
quality as the menu grows (correctness), and it cannot express scoped tool sets
for subagents without a second mechanism anyway (design coherence). Dynamic
loading pays a small upfront complexity cost — a registry and two meta-tools —
to buy all three back. It is load-bearing for property #1, property #2's
scoping, and the cost profile simultaneously, which is exactly the kind of
single decision that earns its place in the MEMO.

# limpiador — Clean Code Rules (CLEAN_CODE.md)

These are the code-style rules every contributor (human or agent) follows on
this repository. They are deliberately short and non-negotiable. The four core
rules come first; the companions after them exist only to support the same
philosophy — small, single-purpose, testable, readable code — and never to
contradict it.

---

## Core rules

### 1. Avoid recursion
Do not use recursion unless the problem is genuinely, irreducibly recursive
(and even then, prefer an explicit stack or iteration where it stays readable).
Recursion hides control flow, complicates stack-depth reasoning, and is harder
to test and trace. In an agent that already orchestrates loops and subagents,
iterative code keeps the execution path inspectable.

### 2. One function does one thing
A function performs a single action. It may take several steps to accomplish
that one action, but it does not accomplish two unrelated actions. "Read the
file AND parse it AND cache it" is three functions. A function named for one
verb that secretly does three is the most common source of untestable code.

### 3. Functions stay under 60 lines
No function exceeds 60 lines. The limit is not arbitrary: a function short
enough to read in one screen is short enough to test, to reason about, and to
hand to another developer without a walkthrough. If a function grows past the
limit, that is the signal it is doing more than one thing (see rule 2) and
should be split.

### 4. Declare variables at the lowest scope required
A variable is declared in the narrowest scope where it is used — not at the top
of the function "for convenience", not at module level if it belongs in a loop.
Narrow scope makes lifetime obvious, prevents accidental reuse, and lets a
reader understand a block without scrolling up to find where a name came from.

---

## Companion rules

These follow directly from the four above and keep the codebase coherent with
limpiador's architecture. They do not override the core rules.

### 5. Functions take and return typed values
Inputs and outputs are typed (pydantic models or explicit type hints), never
ambiguous free text or untyped dicts passed between layers. This is what makes
tool composability real (one tool's output is another's input) and what makes
unit tests assertable. A function whose contract is "returns a dict of
something" is a function nobody can test confidently.

### 6. Errors are typed and raised, not swallowed
Use the `ToolError` hierarchy for failure. Do not return sentinel values like
`None` or `False` to signal an error that a caller might silently ignore, and do
not catch broad exceptions to hide them. An error the agent can read and recover
from is worth more than a crash, and far more than a silent wrong answer.

### 7. No magic values
Thresholds, limits, retry counts, the call ceiling, model names — none of these
are inline literals scattered through the code. They live in named configuration
so they are findable, tunable, and visible to a reviewer. A `30` buried in the
loop tells no one what it means.

### 8. Tests are first-class code
Test code obeys these same rules — single-purpose test functions, under the line
limit, narrow scope, typed fixtures. A test file is not a scratchpad. The test
pyramid in `.clauderules` defines *what* to test; this rule governs *how the
test code itself is written*.

### 9. Names say what, not how
Functions and variables are named for their intent, not their implementation.
`find_references` not `grep_symbol_loop`. When the implementation changes, the
name should still be true. This also keeps `search_tools` ranking legible, since
the model reads tool names to choose them.

---

## The single test
Before committing any function, ask: can someone who has never seen this code
read the function name, read its 60-or-fewer lines once, understand exactly the
one thing it does, and write a test for it without asking a question? If yes, it
passes. If no, it violates one of rules 1–4, and the fix is almost always to
split it.

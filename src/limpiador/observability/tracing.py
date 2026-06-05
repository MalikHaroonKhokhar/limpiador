"""Structured tracing and token accounting (ARCHITECTURE.md §13).

Every tool call and every model call is recorded structurally — which tool, with
what input, returning what, how long it took, how many tokens. The trace is what
the eval harness asserts against (does the agent reason in the right order, under
the call ceiling) and what the demo surfaces. Token accounting lives here;
per-dollar budgeting is deliberately out of scope (§14), but knowing the token
cost of a run is basic observability. Debt-tracker trace tags (e.g.
``[REGISTRY RESEARCH_RETRY]``, ``[CONTEXT REREAD]``) are emitted here so their
frequency can be measured (.clauderules §8).
"""

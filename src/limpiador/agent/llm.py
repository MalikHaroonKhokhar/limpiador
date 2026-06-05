"""The LLM adapter — the ONLY provider-specific file (ARCHITECTURE.md §10).

All OpenAI-specific logic is quarantined behind a single interface: take
messages and the active tool schemas, return a response with text and tool
calls. Two implementations satisfy that interface — the real OpenAI adapter and
the deterministic mock used in tests — so the provider is swappable and the mock
is injectable without the agent core ever knowing which it holds. OpenAI types
must not leak past this module into the loop, the tools, or the schemas
(.clauderules §5).

Model routing (cheap-by-default, escalate-for-planning) and prompt-prefix
stability for caching also live here; the model names and prices are treated as
configuration to be verified against current pricing, never hard-coded.
"""

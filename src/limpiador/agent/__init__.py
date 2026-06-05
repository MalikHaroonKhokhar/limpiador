"""Agent core — the orchestration layer (ARCHITECTURE.md §3, Layer 3).

Holds the loop, the explicit context strategy, the call-count kill-switch, the
provider adapter, and the subagent boundary. It depends on the tool subsystem
and the foundations beneath it, and on nothing above. By construction it never
knows whether it is talking to a real or a mock model — both satisfy the same
adapter interface (:mod:`limpiador.agent.llm`).
"""

"""Dynamic tool loading and search (ARCHITECTURE.md §5, property #1).

The registry holds all fifty-six tools registered at import time, tracks which
are currently loaded into context, and exposes only ``core + loaded`` schemas to
the LLM adapter each turn. The model always sees a small fixed core —
``search_tools(query)``, ``load_tool(name)``, ``finish(result)`` — and discovers
everything else, which is what *proves* model-driven selection: the model cannot
fall back on a tool it was handed because it was handed almost nothing.

Search ranking is a local, deterministic operation over tool names and
descriptions — no model call, no cost. The current keyword-overlap strategy and
its known limitation are tracked as ARCH_DEBT_001 in .clauderules.
"""

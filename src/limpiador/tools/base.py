"""The Tool contract and namespace conventions (ARCHITECTURE.md §5).

Defines the ``Tool`` ABC every tool implements: a namespaced name, a
description used by ``search_tools`` ranking, a typed input/output contract from
:mod:`limpiador.schemas`, and a single ``run`` action (CLEAN_CODE.md §2 — one
function does one thing). A tool emits a typed object and raises a typed
``ToolError`` on failure; it never returns free text and never returns a
sentinel (CLEAN_CODE.md §5-6). Namespaces are load-bearing, not cosmetic: they
are how scoped subagent tool sets are expressed and how search ranking stays
legible (§5.4).
"""

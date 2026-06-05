"""Subagent orchestration (ARCHITECTURE.md §9, property #2).

A subagent is genuinely isolated, not a function call relabelled. It runs in a
fresh context that never sees the parent's history, with a *different* registry
scoped to a read-only tool set, and returns a single typed result across that
isolation boundary. The reviewer (:mod:`limpiador.subagents.reviewer`) is the
first such subagent.
"""

"""Working memory and compaction (ARCHITECTURE.md §7, property #3).

The context object distinguishes durable state (the task, the plan, resolved
sub-goals, symbol-level findings) from transient payloads (the full text a tool
returned). Durable state always survives; transient payloads are summarized and
evicted when they are no longer needed for the immediate next step, so a
twenty-plus-call investigation stays roughly flat in size instead of growing
quadratically.

Compaction is threshold-triggered, not per-call, and the threshold and eviction
policy are named configuration (CLEAN_CODE.md §7) — not magic numbers buried in
the loop. See ARCH_DEBT_002 in .clauderules for the known lossy-summary edge.
"""

"""The call-count kill-switch (ARCHITECTURE.md §6, step 1).

Before each turn, the guard verifies the run has not exceeded its hard ceiling
of tool calls; if it has, the run terminates with a typed ``RunAborted`` result.
This is loop-safety, not budget accounting — a long-horizon agent that loses
coherence fails by looping, and the ceiling is how that failure mode is bounded.
The ceiling is named configuration (CLEAN_CODE.md §7), not a literal in the loop.
"""

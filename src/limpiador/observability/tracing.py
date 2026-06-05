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

from __future__ import annotations

import logging

logger = logging.getLogger("limpiador.trace")

# Debt-tracker trace tags. Counting these in a run's trace is how we measure
# whether a known limitation fires often enough to be worth fixing (.clauderules
# §8). ``[REGISTRY RESEARCH_RETRY]`` is ARCH_DEBT_001: the keyword ranker sent
# the model back to ``search_tools`` instead of letting it load on the first try.
RESEARCH_RETRY_TAG = "[REGISTRY RESEARCH_RETRY]"

# ``[CONTEXT REREAD]`` is ARCH_DEBT_002: a file was read twice in one run — the
# signal that summarize-then-evict dropped a raw payload the agent later needed.
# Keeping symbol facts durable is the mitigation; counting this tag is how we
# learn whether re-reads stay rare enough to leave that mitigation as-is.
CONTEXT_REREAD_TAG = "[CONTEXT REREAD]"

# ``[ROUTING]`` is an observability tag, not a debt one: every model call records
# which turn kind it was, which model served it, and the stable-prefix fingerprint
# that prompt caching keys on. Counting these is how a run shows the bulk of calls
# went to the cheap model and that the cached head stayed stable across turns.
ROUTING_TAG = "[ROUTING]"


def emit(tag: str, message: str = "") -> None:
    """Record a tagged trace event so its frequency can be counted later.

    Intentionally thin: it logs through the ``limpiador.trace`` logger so a tag
    surfaces in a run trace without any subsystem depending on a richer tracer.
    Components that need to assert on emissions in a test inject their own
    callable instead of reaching for this default.
    """
    if message:
        logger.info("%s %s", tag, message)
    else:
        logger.info("%s", tag)

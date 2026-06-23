"""RED HERRING — the innocent, most-recently-touched file in the repo and the
obvious suspect, but it is correct and has nothing to do with the failure. A
recency- or blame-driven agent looks here first; the real cause is
pipeline.normalize. The eval asserts this file is left byte-for-byte unchanged.

Recently refactored: extracted the defaults into a dict (no behaviour change).
"""

DEFAULTS = {"trim": True, "encoding": "utf-8"}


def merged(overrides):
    return {**DEFAULTS, **overrides}

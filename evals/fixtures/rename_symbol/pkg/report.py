"""A third file that uses `compute` — the rename must reach this call site too.
Together with core.py and consumer.py this makes three known sites for one
symbol, so an incomplete rename is detectable."""

from pkg.core import compute


def summary(values):
    return {"total": sum(compute(v) for v in values), "count": len(values)}

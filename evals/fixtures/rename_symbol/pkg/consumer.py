"""A second file that uses `compute` — the rename must reach this call site too,
or the build breaks."""

from pkg.core import compute


def total(values):
    return sum(compute(v) for v in values)

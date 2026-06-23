"""Public-API smoke test for the rename_symbol fixture.

It exercises the package's public surface (``run``/``total``/``summary``) without
naming the symbol under rename, so it is *not* itself a rename site — yet it
breaks the moment any call site of the renamed function is missed, which is what
makes the ``safe_rename`` case's "tests pass" clause meaningful.
"""

from pkg.consumer import total
from pkg.core import run
from pkg.report import summary


def test_public_api_is_intact():
    assert run() == 15
    assert total([1, 2]) == 23
    assert summary([1, 2]) == {"total": 23, "count": 2}

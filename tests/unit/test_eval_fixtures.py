"""Ground-truth self-tests for the seeded eval fixtures (HAR-31).

Each committed fixture under ``evals/fixtures/`` carries a *known, planted*
defect — that is what makes an eval assertion binary. These tests are the
fixtures' contract, proving the three things HAR-31 promises without ever calling
a model:

* every fixture **loads via the harness checkout** as a fresh, isolated git repo;
* every fixture documents its **ground truth** in a per-fixture ``README.md``;
* every fixture's **defect is real and reproducible** — it shows up offline, and
  the documented fix (or the documented invariant) flips it cleanly.

The defects, stated as the per-fixture READMEs state them:

* ``failing_test``  — ``calc.add`` subtracts instead of adding; one test fails.
* ``rename_symbol`` — ``compute`` is defined once and used across three files.
* ``bad_pr``        — ``pr.diff`` flips ``+`` to ``-`` in ``apply_restock``, a
                      regression a reviewer must reject.
* ``red_herring``   — ``pipeline.normalize`` drops the first row; the innocent,
                      recently-touched ``settings.py`` beside it is a distractor.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from evals.harness import checkout_fixture

_FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"

# The four fixtures HAR-31 commits, by directory name.
_ALL_FIXTURES = ("failing_test", "rename_symbol", "bad_pr", "red_herring")


@contextmanager
def _checkout(name: str) -> Iterator[Path]:
    """A fresh, isolated checkout of a fixture, removed on exit."""
    path = checkout_fixture(name)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _tests_fail(checkout: Path) -> bool:
    """True iff pytest is RED in the checkout (the seeded defect is live)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(checkout)],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    return proc.returncode != 0


def _tests_pass(checkout: Path) -> bool:
    return not _tests_fail(checkout)


# ---- every fixture loads and documents its ground truth ----------------------
@pytest.mark.parametrize("name", _ALL_FIXTURES)
def test_fixture_loads_as_a_fresh_isolated_repo(name: str) -> None:
    with _checkout(name) as checkout:
        # A real, materialised git repo (the agent's git tools need one)...
        assert (checkout / ".git").is_dir()
        # ...that is an independent copy: scribbling on it never touches the source.
        (checkout / "SCRIBBLE.txt").write_text("transient")
        assert not (_FIXTURES / name / "SCRIBBLE.txt").exists()


@pytest.mark.parametrize("name", _ALL_FIXTURES)
def test_fixture_documents_its_ground_truth(name: str) -> None:
    readme = _FIXTURES / name / "README.md"
    assert readme.is_file(), f"{name} must document its planted defect in README.md"
    assert "ground truth" in readme.read_text().lower()


# ---- failing_test: one test fails for a planted reason -----------------------
def test_failing_test_defect_is_real_and_the_documented_fix_resolves_it() -> None:
    with _checkout("failing_test") as checkout:
        assert _tests_fail(checkout), "the seeded calc.add bug must fail the suite"

        # The README's fix: add must add, not subtract.
        calc = checkout / "calc.py"
        calc.write_text(calc.read_text().replace("return a - b", "return a + b"))
        assert _tests_pass(checkout), "the documented fix must make the suite green"


def test_failing_test_red_herring_is_innocent() -> None:
    # formatter.py is unrelated: fixing only calc.py turns the suite green while
    # the herring is left byte-for-byte untouched.
    with _checkout("failing_test") as checkout:
        calc = checkout / "calc.py"
        calc.write_text(calc.read_text().replace("return a - b", "return a + b"))
        herring = (checkout / "formatter.py").read_text()
        assert _tests_pass(checkout)
        assert herring == (_FIXTURES / "failing_test" / "formatter.py").read_text()


# ---- rename_symbol: defined once, used across three files --------------------
def test_rename_symbol_is_defined_once_and_used_across_three_files() -> None:
    with _checkout("rename_symbol") as checkout:
        pkg = checkout / "pkg"
        sources = {p.name: p.read_text() for p in pkg.glob("*.py")}

        definitions = sum(src.count("def compute(") for src in sources.values())
        assert definitions == 1, "compute must be defined exactly once (the ground truth)"

        users = {name for name, src in sources.items() if "compute" in src}
        assert users == {"core.py", "consumer.py", "report.py"}, (
            "compute must be used across exactly the three known files"
        )


# ---- bad_pr: a PR diff carrying a deliberate regression ----------------------
def test_bad_pr_diff_applies_in_reverse_to_a_green_base() -> None:
    # The working tree IS the proposed (regressed) state, so the suite is RED;
    # reverse-applying pr.diff recovers the base, which is GREEN. That round-trip
    # proves the diff is *exactly* what introduced the regression.
    with _checkout("bad_pr") as checkout:
        assert (checkout / "pr.diff").is_file()
        assert _tests_fail(checkout), "the proposed PR state must regress the suite"

        revert = subprocess.run(
            ["git", "apply", "--reverse", "pr.diff"],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        assert revert.returncode == 0, f"pr.diff must reverse cleanly: {revert.stderr}"
        assert _tests_pass(checkout), "the pre-PR base must be green"


# ---- red_herring: the real cause beside a recently-touched distractor --------
def test_red_herring_real_cause_is_the_broken_file_not_the_distractor() -> None:
    with _checkout("red_herring") as checkout:
        assert _tests_fail(checkout), "the seeded pipeline bug must fail the suite"

        # The README's fix lives in pipeline.py — and nowhere near settings.py.
        pipeline = checkout / "pipeline.py"
        pipeline.write_text(pipeline.read_text().replace("rows[1:]", "rows"))
        assert _tests_pass(checkout), "fixing the real cause alone must turn it green"

        # The recently-touched 'suspect' was innocent the whole time.
        assert (checkout / "settings.py").read_text() == (
            _FIXTURES / "red_herring" / "settings.py"
        ).read_text()

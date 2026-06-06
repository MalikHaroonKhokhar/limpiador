"""Layer 3 — real OpenAI + real git/github through the CLI (.clauderules L3).

These tests exercise the *pipeline and plumbing*, not the agent's reasoning:
invoke the CLI as a subprocess (not in-process), let it parse args, boot, pick
the real run mode, authenticate, drive real git/github, and exit cleanly. They
run only against the throwaway ``LIMPIADOR_SANDBOX_REPO`` — never a real repo —
and skip cleanly unless ``OPENAI_API_KEY`` / ``GITHUB_TOKEN`` /
``LIMPIADOR_SANDBOX_REPO`` are all set, so CI stays green and no credit is spent.

Assertions are deliberately relaxed (the real model is non-deterministic): a
clean exit code, a commit that landed, a PR that appeared — the plumbing worked
— never an exact transcript. The git namespace has no ``push`` tool, so the
"land a commit / open a PR" flow is exercised in faithful slices: the CLI lands
a real local commit (``test_cli_lands_a_real_commit``) and opens a real PR for a
head branch already on the sandbox (``test_cli_opens_a_real_pr_on_the_sandbox``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from git import Repo
from github import Auth, Github, GithubException

_REQUIRED_ENV = ("OPENAI_API_KEY", "GITHUB_TOKEN", "LIMPIADOR_SANDBOX_REPO")
_MISSING = [name for name in _REQUIRED_ENV if not os.environ.get(name)]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        bool(_MISSING),
        reason=f"L3 e2e needs real credentials + a throwaway repo; missing: {_MISSING}",
    ),
]

_CLI_TIMEOUT_S = 300


# ---- helpers: the sandbox, an authed clone, and the CLI subprocess ----------
def _slug() -> str:
    return os.environ["LIMPIADOR_SANDBOX_REPO"]


def _authed_url() -> str:
    """The sandbox clone URL with the token embedded, so push authenticates."""
    return f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{_slug()}.git"


def _stamp() -> str:
    """A unique, sortable tag so concurrent or repeated runs never collide."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _clone(dest: Path) -> Repo:
    """Clone the sandbox into a temp dir with a deterministic commit identity."""
    repo = Repo.clone_from(_authed_url(), dest)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "E2E Harness")
        writer.set_value("user", "email", "e2e@example.com")
    return repo


def _run_cli(repo: Path, task: str, *, max_calls: int = 20) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a real subprocess (not in-process) in real mode."""
    env = {**os.environ, "LIMPIADOR_LLM": "openai", "GITHUB_REPOSITORY": _slug()}
    return subprocess.run(
        [
            sys.executable, "-m", "limpiador.cli", "run",
            "--repo", str(repo), "--task", task, "--max-calls", str(max_calls),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )


def _sandbox():
    return Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"])).get_repo(_slug())


# ---- 1) boot → auth → clean exit (read-only, pure plumbing) ------------------
def test_cli_boots_authenticates_and_exits_cleanly(tmp_path) -> None:
    clone = _clone(tmp_path / "sandbox")

    proc = _run_cli(
        Path(clone.working_tree_dir),
        "Report the repository's current branch using git status, then finish.",
        max_calls=8,
    )

    # The whole pipeline ran end to end: parsed args, booted, authenticated to the
    # real model, dispatched tools, and exited cleanly. That is the plumbing.
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ---- 2) the CLI lands a real commit (real git through the agent) -------------
def test_cli_lands_a_real_commit(tmp_path) -> None:
    clone = _clone(tmp_path / "sandbox")
    before = clone.head.commit.hexsha
    stamp = _stamp()

    proc = _run_cli(
        Path(clone.working_tree_dir),
        f"Create a file named e2e-{stamp}.md containing the text 'e2e {stamp}', "
        f"stage it, and commit it with the message 'e2e {stamp}'. Then finish.",
        max_calls=16,
    )

    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # Relaxed: a new local commit landed (the agent wrote → staged → committed via
    # real git). The clone is local only, so the sandbox is untouched here.
    after = Repo(clone.working_tree_dir).head.commit.hexsha
    assert after != before, f"expected a new commit; stdout={proc.stdout}"


# ---- 3) the CLI opens a real PR on the sandbox -------------------------------
def test_cli_opens_a_real_pr_on_the_sandbox(tmp_path) -> None:
    sandbox = _sandbox()
    base = sandbox.default_branch
    stamp = _stamp()
    branch = f"e2e/pr-{stamp}"

    # Prepare the head the PR needs: a real branch + commit pushed to the sandbox.
    clone = _clone(tmp_path / "sandbox")
    clone.git.checkout("-b", branch)
    (Path(clone.working_tree_dir) / f"e2e-{stamp}.md").write_text(f"e2e {stamp}\n")
    clone.index.add([f"e2e-{stamp}.md"])
    clone.index.commit(f"e2e {stamp}")
    clone.git.push("origin", branch)

    pr = None
    try:
        title = f"E2E test PR {stamp}"
        proc = _run_cli(
            Path(clone.working_tree_dir),
            f"Open a pull request from the branch '{branch}' into '{base}' titled "
            f"'{title}' with the body 'automated e2e'. Then finish.",
            max_calls=16,
        )

        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        # Relaxed acceptance: a PR for our head branch now exists on the sandbox.
        opened = list(sandbox.get_pulls(state="open", head=f"{sandbox.owner.login}:{branch}"))
        assert opened, f"expected a PR for head {branch}; stdout={proc.stdout}"
        pr = opened[0]
        assert pr.base.ref == base
    finally:
        # Leave the throwaway repo as we found it: close the PR and drop the branch.
        if pr is not None:
            pr.edit(state="closed")
        try:
            sandbox.get_git_ref(f"heads/{branch}").delete()
        except GithubException:
            pass

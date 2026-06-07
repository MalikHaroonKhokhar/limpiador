"""``git.*`` namespace — local repository state + publishing (ARCHITECTURE.md §5.3, 13 tools).

status, diff, log, show, branch_list, branch_create, checkout, stage, commit,
reset, stash, push, blame. Backed by real git (gitpython); each tool emits a
typed object from :mod:`limpiador.schemas` and raises a typed ``ToolError`` on
failure. ``push`` is the one that reaches a remote — it is what lets the agent
publish a branch so a pull request can be opened against it.

The request schemas carry no repository path: a git tool resolves the repo
*ambiently* from the current working directory (``_open_repo``), the same way a
person running ``git`` in a checkout does. That is what lets the thirteen stay
module-level singletons registered once at import — the CLI anchors the agent to
``--repo`` by running there, and every tool then operates on that one checkout.

A git ref/path miss reaches us as one of several gitpython exception types
depending on which plumbing answered; they all mean the same thing to a caller
(*the thing you named is not here*), so they fold into a single typed
``NotFoundError`` the agent can read and adapt to, rather than a raw traceback.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from git import (
    BadName,
    BadObject,
    Commit,
    GitCommandError,
    InvalidGitRepositoryError,
    NoSuchPathError,
    Repo,
)

from limpiador.observability.errors import MalformedInputError, NotFoundError
from limpiador.schemas import (
    BlameLine,
    CommitInfo,
    GitBlameRequest,
    GitBlameResult,
    GitBranchCreateRequest,
    GitBranchCreateResult,
    GitBranchListRequest,
    GitBranchListResult,
    GitCheckoutRequest,
    GitCheckoutResult,
    GitCommitRequest,
    GitCommitResult,
    GitDiffRequest,
    GitDiffResult,
    GitLogRequest,
    GitLogResult,
    GitPushRequest,
    GitPushResult,
    GitResetRequest,
    GitResetResult,
    GitShowRequest,
    GitShowResult,
    GitStageRequest,
    GitStageResult,
    GitStashRequest,
    GitStashResult,
    GitStatusRequest,
    GitStatusResult,
)
from limpiador.tools.base import Tool

# The gitpython exceptions that all reduce to "the ref/path you named is not
# here": a failed git subcommand, a bad rev name/object, and the value/OS errors
# raised when resolving an empty history or a missing working-tree path.
_LOOKUP_ERRORS = (GitCommandError, BadName, BadObject, ValueError, OSError)
_DEFAULT_PROTECTED_BRANCHES = ("main", "master")
_PROTECTED_BRANCHES_ENV = "LIMPIADOR_PROTECTED_BRANCHES"
_ALLOW_PROTECTED_WRITES_ENV = "LIMPIADOR_ALLOW_PROTECTED_BRANCH_WRITES"


# ---- ambient repository + shared projections --------------------------------
def _open_repo() -> Repo:
    """Open the repository that contains the current working directory."""
    try:
        return Repo(Path.cwd(), search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError) as error:
        raise NotFoundError(
            "no git repository at the current working directory"
        ) from error


@contextmanager
def _translating(detail: str) -> Iterator[None]:
    """Fold a gitpython ref/path lookup failure into a typed ``NotFoundError``."""
    try:
        yield
    except _LOOKUP_ERRORS as error:
        raise NotFoundError(f"{detail}: {error}") from error


def _current_ref(repo: Repo) -> str:
    """The checked-out branch name, or the short HEAD sha when detached."""
    try:
        return repo.active_branch.name
    except TypeError:
        return repo.head.commit.hexsha[:12]


def _protected_branches() -> set[str]:
    """Branch names the agent must not write unless explicitly unlocked."""
    configured = os.environ.get(_PROTECTED_BRANCHES_ENV)
    if configured is None:
        return set(_DEFAULT_PROTECTED_BRANCHES)
    return {name.strip() for name in configured.split(",") if name.strip()}


def _protected_writes_allowed() -> bool:
    """Whether branch protection has been intentionally bypassed."""
    return os.environ.get(_ALLOW_PROTECTED_WRITES_ENV) == "1"


def _reject_protected_write(action: str, branch: str) -> None:
    """Stop accidental direct writes to shared default branches."""
    if _protected_writes_allowed() or branch not in _protected_branches():
        return
    raise MalformedInputError(
        f"refusing to {action} protected branch {branch!r}; "
        "create and check out a feature branch first."
    )


def _reject_protected_push_ref(ref: str) -> None:
    """Stop protected branch pushes named as branches or refspecs."""
    if _protected_writes_allowed():
        return
    names = [_short_ref(part) for part in ref.split(":") if part]
    blocked = [name for name in names if name in _protected_branches()]
    if blocked:
        _reject_protected_write("push", blocked[0])


def _short_ref(ref: str) -> str:
    """Normalize a branch or refs/heads ref to its branch name."""
    ref = ref.removeprefix("+")
    return ref.removeprefix("refs/heads/")


def _commit_info(commit: Commit) -> CommitInfo:
    """Project a gitpython commit onto the typed :class:`CommitInfo` contract."""
    return CommitInfo(
        sha=commit.hexsha,
        message=str(commit.message).strip() or "(no message)",
        author=commit.author.name or commit.author.email or "unknown",
        date=commit.committed_datetime.isoformat(),
    )


def _staged_paths(repo: Repo) -> list[str]:
    """Paths staged for the next commit (index vs HEAD, or all on an unborn HEAD)."""
    if not repo.head.is_valid():
        return sorted({key[0] for key in repo.index.entries})
    return sorted((diff.a_path or diff.b_path) for diff in repo.index.diff(repo.head.commit))


def _names_only(repo: Repo, *args: str) -> list[str]:
    """The paths a diff touches, as a clean list (``git diff --name-only``)."""
    output = repo.git.diff("--name-only", *args)
    return [line for line in output.splitlines() if line]


# ---- the thirteen tools -------------------------------------------------------
class GitStatus(Tool):
    name = "git.status"
    description = (
        "Show the working-tree status of the repository: the current branch and "
        "the staged, unstaged, and untracked paths. Synonyms: state, dirty, "
        "modified, changes, what changed, uncommitted work."
    )
    Input = GitStatusRequest
    Output = GitStatusResult

    def run(self, request: GitStatusRequest) -> GitStatusResult:
        repo = _open_repo()
        return GitStatusResult(
            branch=_current_ref(repo),
            staged=_staged_paths(repo),
            unstaged=sorted(diff.a_path for diff in repo.index.diff(None)),
            untracked=sorted(repo.untracked_files),
            clean=not repo.is_dirty(untracked_files=True),
        )


class GitDiff(Tool):
    name = "git.diff"
    description = (
        "Show repository diffs for a ref, a path, or the staged index. Synonyms: "
        "delta, changes, patch, what changed, compare, unified diff."
    )
    Input = GitDiffRequest
    Output = GitDiffResult

    def run(self, request: GitDiffRequest) -> GitDiffResult:
        repo = _open_repo()
        args = (["--cached"] if request.staged else []) + (
            [request.ref] if request.ref else []
        )
        path = ["--", request.path] if request.path else []
        with _translating(f"cannot diff {request.ref or 'the working tree'!r}"):
            diff = repo.git.diff(*args, *path)
            files = _names_only(repo, *args, *path)
        return GitDiffResult(diff=diff, files_changed=files)


class GitLog(Tool):
    name = "git.log"
    description = (
        "List recent commits, newest first, optionally limited to a path. "
        "Synonyms: history, commits, changelog, revisions, who changed what."
    )
    Input = GitLogRequest
    Output = GitLogResult

    def run(self, request: GitLogRequest) -> GitLogResult:
        repo = _open_repo()
        with _translating("cannot read commit history"):
            commits = list(
                repo.iter_commits(paths=request.path or "", max_count=request.max_count)
            )
        return GitLogResult(commits=[_commit_info(commit) for commit in commits])


class GitShow(Tool):
    name = "git.show"
    description = (
        "Show one commit or ref: its commit metadata and the diff it introduced. "
        "Synonyms: inspect commit, reveal, display, what did this commit change."
    )
    Input = GitShowRequest
    Output = GitShowResult

    def run(self, request: GitShowRequest) -> GitShowResult:
        repo = _open_repo()
        with _translating(f"no such commit {request.ref!r}"):
            commit = repo.commit(request.ref)
            diff = repo.git.show(request.ref, format="", no_color=True)
        return GitShowResult(commit=_commit_info(commit), diff=diff)


class GitBranchList(Tool):
    name = "git.branch_list"
    description = (
        "List the local branches and identify the current one. Synonyms: "
        "branches, refs, heads, which branch am I on, enumerate branches."
    )
    Input = GitBranchListRequest
    Output = GitBranchListResult

    def run(self, request: GitBranchListRequest) -> GitBranchListResult:
        repo = _open_repo()
        return GitBranchListResult(
            branches=sorted(head.name for head in repo.heads),
            current=_current_ref(repo),
        )


class GitBranchCreate(Tool):
    name = "git.branch_create"
    description = (
        "Create a local branch from an optional base ref and switch to it, so "
        "your next commit lands on the new branch (like git switch -c). Synonyms: "
        "new branch, fork, make branch, branch off, start a branch."
    )
    Input = GitBranchCreateRequest
    Output = GitBranchCreateResult

    def run(self, request: GitBranchCreateRequest) -> GitBranchCreateResult:
        repo = _open_repo()
        with _translating(f"cannot create branch from {request.base!r}"):
            if request.base:
                repo.create_head(request.name, commit=request.base)
            else:
                repo.create_head(request.name)
            # Switch onto it: "make a branch named X" means start working on X.
            # Creating without switching left the agent on the protected default
            # branch, where every commit was correctly rejected and the run stalled.
            repo.git.checkout(request.name)
        return GitBranchCreateResult(name=request.name, created=True, switched=True)


class GitCheckout(Tool):
    name = "git.checkout"
    description = (
        "Check out an existing ref, or create and check out a new branch. "
        "Synonyms: switch, change branch, move to, go to branch, select ref."
    )
    Input = GitCheckoutRequest
    Output = GitCheckoutResult

    def run(self, request: GitCheckoutRequest) -> GitCheckoutResult:
        repo = _open_repo()
        previous = _current_ref(repo)
        with _translating(f"cannot check out {request.ref!r}"):
            if request.create:
                if request.base:
                    repo.create_head(request.ref, commit=request.base)
                else:
                    repo.create_head(request.ref)
            repo.git.checkout(request.ref)
        return GitCheckoutResult(ref=request.ref, previous=previous)


class GitStage(Tool):
    name = "git.stage"
    description = (
        "Stage one or more paths for the next commit. Synonyms: add, git add, "
        "track, index, prepare for commit."
    )
    Input = GitStageRequest
    Output = GitStageResult

    def run(self, request: GitStageRequest) -> GitStageResult:
        repo = _open_repo()
        with _translating("cannot stage paths"):
            repo.index.add(list(request.paths))
        return GitStageResult(staged=list(request.paths))


class GitCommit(Tool):
    name = "git.commit"
    description = (
        "Commit the staged changes with a message and return the new commit. "
        "Synonyms: record, save, snapshot, check in, seal changes."
    )
    Input = GitCommitRequest
    Output = GitCommitResult

    def run(self, request: GitCommitRequest) -> GitCommitResult:
        repo = _open_repo()
        if not _staged_paths(repo):
            raise MalformedInputError("nothing staged to commit; stage changes first.")
        _reject_protected_write("commit on", _current_ref(repo))
        commit = repo.index.commit(request.message)
        return GitCommitResult(sha=commit.hexsha, message=request.message)


class GitReset(Tool):
    name = "git.reset"
    description = (
        "Reset the repository to a ref, optionally hard (discarding the working "
        "tree). Synonyms: rewind, undo, roll back, unstage, move HEAD."
    )
    Input = GitResetRequest
    Output = GitResetResult

    def run(self, request: GitResetRequest) -> GitResetResult:
        repo = _open_repo()
        args = ["--hard", request.ref] if request.hard else [request.ref]
        with _translating(f"cannot reset to {request.ref!r}"):
            repo.git.reset(*args)
        return GitResetResult(ref=request.ref)


class GitStash(Tool):
    name = "git.stash"
    description = (
        "Create or pop a git stash entry. Synonyms: shelve, set aside, save for "
        "later, restore stash, park changes."
    )
    Input = GitStashRequest
    Output = GitStashResult

    def run(self, request: GitStashRequest) -> GitStashResult:
        repo = _open_repo()
        if request.pop:
            with _translating("no stash entry to pop"):
                repo.git.stash("pop")
            return GitStashResult(stash_ref=None, popped=True)
        args = ["push"] + (["-m", request.message] if request.message else [])
        output = repo.git.stash(*args)
        created = "No local changes" not in output
        return GitStashResult(stash_ref="stash@{0}" if created else None, popped=False)


class GitPush(Tool):
    name = "git.push"
    description = (
        "Push a local branch to a remote, optionally setting upstream or forcing. "
        "Synonyms: publish, upload, send to remote, git push, push branch, share."
    )
    Input = GitPushRequest
    Output = GitPushResult

    def run(self, request: GitPushRequest) -> GitPushResult:
        repo = _open_repo()
        branch = request.branch or _current_ref(repo)
        _reject_protected_push_ref(branch)
        flags = (["--set-upstream"] if request.set_upstream else []) + (
            ["--force"] if request.force else []
        )
        with _translating(f"cannot push {branch!r} to {request.remote!r}"):
            repo.git.push(*flags, request.remote, branch)
        return GitPushResult(remote=request.remote, branch=branch, pushed=True)


class GitBlame(Tool):
    name = "git.blame"
    description = (
        "Blame a file or line range to identify the commits responsible. "
        "Synonyms: annotate, who wrote this, last touched, attribution, praise."
    )
    Input = GitBlameRequest
    Output = GitBlameResult

    def run(self, request: GitBlameRequest) -> GitBlameResult:
        repo = _open_repo()
        with _translating(f"cannot blame {request.file!r}"):
            blame = repo.blame("HEAD", request.file)
        lines = _blame_lines(blame)
        return GitBlameResult(
            file=request.file,
            lines=_within_range(lines, request.line_start, request.line_end),
        )


def _blame_lines(blame: object) -> list[BlameLine]:
    """Flatten gitpython blame hunks into numbered :class:`BlameLine` rows."""
    rows: list[BlameLine] = []
    number = 1
    for commit, texts in blame:  # type: ignore[union-attr]
        for text in texts:
            rows.append(
                BlameLine(
                    line=number,
                    sha=commit.hexsha,
                    author=commit.author.name or "unknown",
                    content=text,
                )
            )
            number += 1
    return rows


def _within_range(
    lines: list[BlameLine], start: int | None, end: int | None
) -> list[BlameLine]:
    """Keep only the blame rows inside an inclusive ``[start, end]`` line range."""
    if start is None and end is None:
        return lines
    low = start or 1
    high = end if end is not None else (lines[-1].line if lines else 0)
    return [row for row in lines if low <= row.line <= high]


TOOLS = (
    GitStatus(),
    GitDiff(),
    GitLog(),
    GitShow(),
    GitBranchList(),
    GitBranchCreate(),
    GitCheckout(),
    GitStage(),
    GitCommit(),
    GitReset(),
    GitStash(),
    GitPush(),
    GitBlame(),
)

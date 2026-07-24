"""``github.*`` namespace — remote collaboration (ARCHITECTURE.md §5.3, 14 tools).

get_issue, list_issues, create_issue, comment_issue, get_pr, list_prs,
create_pr, review_pr, request_changes, merge_pr, list_checks, get_check_logs,
get_file_at_ref, search_code. Backed by the real GitHub API (pygithub).

Every executor routes its API call through the one resilient boundary in
``github_client.GitHubBoundary`` (HAR-16): the retry/backoff and the token-bucket
rate limiter live in that single place, not scattered across the fourteen tools.
A transient failure (a 429, a 5xx, a dropped connection) is backed off and
retried there, and a persistent one gives up as a typed ``TransientError``. The
non-transient failures the agent must *read* — a 404, a 403 — come back out of
the boundary as raw ``GithubException``, which the tools translate into the
matching typed ``ToolError`` (``NotFoundError`` / ``PermissionDeniedError``).

Auth and target repository are resolved ambiently, the same shape as the
``git.*`` tools: the token from ``GITHUB_TOKEN`` and the ``owner/name`` slug from
``GITHUB_REPOSITORY`` or the working tree's ``origin`` remote. That resolution is
lazy — built on first call, never at import — so the tools stay module-level
singletons and ``make test`` needs no credentials. Tests inject their own
:class:`GitHubSession` (a fake client on a fake clock) to drive the tools with no
network.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from git import InvalidGitRepositoryError, NoSuchPathError, Repo
from github import Auth, Github, GithubException

from limpiador.observability.errors import (
    ConfigError,
    MalformedInputError,
    NotFoundError,
    PermissionDeniedError,
    ToolError,
)
from limpiador.schemas import (
    CheckRun,
    CodeMatch,
    GithubCheckList,
    GithubCheckLogs,
    GithubCodeSearchResult,
    GithubComment,
    GithubCommentIssueRequest,
    GithubCreateIssueRequest,
    GithubCreatePrRequest,
    GithubFileContent,
    GithubGetCheckLogsRequest,
    GithubGetFileAtRefRequest,
    GithubGetIssueRequest,
    GithubGetPrRequest,
    GithubIssueList,
    GithubListChecksRequest,
    GithubListIssuesRequest,
    GithubListPrsRequest,
    GithubMergePrRequest,
    GithubMergeResult,
    GithubPrList,
    GithubRequestChangesRequest,
    GithubReviewPrRequest,
    GithubReviewSubmission,
    GithubSearchCodeRequest,
    Issue,
    PullRequest,
    Verdict,
)
from limpiador.tools.base import Tool
from limpiador.tools.github_client import GitHubBoundary

T = TypeVar("T")

# The ambient configuration sources (mirrors git.* resolving the repo from cwd).
_TOKEN_ENV = "GITHUB_TOKEN"
_REPO_ENV = "GITHUB_REPOSITORY"

# A reviewer verdict maps onto GitHub's review event vocabulary.
_REVIEW_EVENTS = {
    Verdict.APPROVE: "APPROVE",
    Verdict.REQUEST_CHANGES: "REQUEST_CHANGES",
    Verdict.COMMENT: "COMMENT",
}


# ---- the session: how a tool reaches the API and the resilient boundary -----
@dataclass(frozen=True)
class GitHubSession:
    """What a ``github.*`` tool needs to act: a client factory and the boundary.

    The client is produced by a factory (not held directly) so a tool resolves
    the live ``Repository`` lazily, inside the metered/retried boundary call. A
    test supplies a fake factory and a boundary on a fake clock; production uses
    the lazily-built default below.
    """

    boundary: GitHubBoundary
    client_factory: Callable[[], Github]
    slug: str

    def client(self) -> Github:
        return self.client_factory()

    def repo(self):
        return self.client_factory().get_repo(self.slug)


_DEFAULT_SESSION: GitHubSession | None = None


def _default_session() -> GitHubSession:
    """The shared production session, built once from the ambient credentials."""
    # A deliberate module-level singleton: the session holds one authenticated
    # client, and rebuilding it per call would re-authenticate on every tool use.
    global _DEFAULT_SESSION  # noqa: PLW0603 - intentional lazy cache
    if _DEFAULT_SESSION is None:
        _DEFAULT_SESSION = _build_default_session()
    return _DEFAULT_SESSION


def _build_default_session() -> GitHubSession:
    token = os.environ.get(_TOKEN_ENV)
    if not token:
        raise ConfigError(
            f"{_TOKEN_ENV} is not set; the github.* tools require a token. "
            "Use mock mode for offline development."
        )
    client = Github(auth=Auth.Token(token))
    return GitHubSession(
        boundary=GitHubBoundary(),
        client_factory=lambda: client,
        slug=_resolve_slug(),
    )


def _resolve_slug() -> str:
    """The ``owner/name`` slug from the env, or parsed from the origin remote."""
    slug = os.environ.get(_REPO_ENV) or _slug_from_origin()
    if not slug:
        raise ConfigError(
            f"set {_REPO_ENV} to 'owner/name' or run inside a checkout with a "
            "github origin remote so the github.* tools know which repository to use."
        )
    return slug


def _slug_from_origin() -> str | None:
    """Best-effort ``owner/name`` from the working tree's ``origin`` remote URL."""
    try:
        origin_url = Repo(Path.cwd(), search_parent_directories=True).remotes.origin.url
    except (InvalidGitRepositoryError, NoSuchPathError, AttributeError):
        origin_url = None  # not a repo, or no origin remote — fall back to the env var
    if origin_url is None:
        return None
    url = origin_url.removesuffix(".git")
    tail = url.split(":", 1)[-1] if url.startswith("git@") else url.split("github.com/", 1)[-1]
    return tail or None


# ---- failure translation: non-transient GitHub errors -> typed ToolError ----
def _classify(error: GithubException) -> ToolError:
    """Map a non-transient GitHub failure onto the typed error the agent reads."""
    if error.status == 404:
        return NotFoundError(f"github: resource not found ({_message(error)})")
    if error.status in (401, 403):
        return PermissionDeniedError(f"github: not permitted ({_message(error)})")
    return MalformedInputError(f"github: request rejected ({error.status}: {_message(error)})")


def _message(error: GithubException) -> str:
    data = error.data
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    return str(error)


# ---- shared projections onto the typed schemas ------------------------------
def _to_issue(issue: object) -> Issue:
    return Issue(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        body=issue.body,
        author=issue.user.login if issue.user else None,
        labels=[label.name for label in issue.labels],
    )


def _to_pull(pull: object, *, changed_files: list[str], diff: str | None = None) -> PullRequest:
    return PullRequest(
        number=pull.number,
        title=pull.title,
        state=pull.state,
        head_ref=pull.head.ref,
        base_ref=pull.base.ref,
        body=pull.body,
        diff=diff,
        changed_files=changed_files,
    )


def _diff_from_files(files: list[object]) -> str | None:
    """Assemble a unified diff from a PR's per-file patches (the get_pr → reviewer
    handoff): the concatenated patches are the ``diff`` that feeds spawn_reviewer."""
    patches = [file.patch for file in files if getattr(file, "patch", None)]
    return "\n".join(patches) if patches else None


# ---- the base every github tool shares --------------------------------------
class _GitHubTool(Tool):
    """Shared base: resolves the session and routes one call through the boundary."""

    def __init__(self, session: GitHubSession | None = None) -> None:
        self._session = session

    def _active(self) -> GitHubSession:
        return self._session if self._session is not None else _default_session()

    def _repo(self):
        return self._active().repo()

    def _call(self, operation: Callable[[], T]) -> T:
        """Run one API operation through the resilient boundary, typing its errors.

        The boundary backs off and retries transient failures and gives up as a
        typed ``TransientError``; a non-transient ``GithubException`` propagates
        here, where it becomes the matching typed ``ToolError`` the loop folds.
        """
        try:
            return self._active().boundary.call(operation)
        except GithubException as error:
            raise _classify(error) from error


# ---- the fourteen tools -----------------------------------------------------
class GithubGetIssue(_GitHubTool):
    name = "github.get_issue"
    description = (
        "Fetch a single issue by its number. Synonyms: read issue, show issue, "
        "look up ticket, get bug report."
    )
    Input = GithubGetIssueRequest
    Output = Issue

    def run(self, request: GithubGetIssueRequest) -> Issue:
        issue = self._call(lambda: self._repo().get_issue(request.number))
        return _to_issue(issue)


class GithubListIssues(_GitHubTool):
    name = "github.list_issues"
    description = (
        "List issues, optionally filtered by state or labels. Synonyms: enumerate "
        "issues, find tickets, browse bugs, open issues."
    )
    Input = GithubListIssuesRequest
    Output = GithubIssueList

    def run(self, request: GithubListIssuesRequest) -> GithubIssueList:
        def operation():
            repo = self._repo()
            if request.labels:
                return list(repo.get_issues(state=request.state, labels=request.labels))
            return list(repo.get_issues(state=request.state))

        issues = self._call(operation)
        return GithubIssueList(issues=[_to_issue(issue) for issue in issues])


class GithubCreateIssue(_GitHubTool):
    name = "github.create_issue"
    description = (
        "Open a new issue with a title, body, and labels. Synonyms: file a bug, "
        "report a problem, raise a ticket, new issue."
    )
    Input = GithubCreateIssueRequest
    Output = Issue

    def run(self, request: GithubCreateIssueRequest) -> Issue:
        issue = self._call(
            lambda: self._repo().create_issue(
                title=request.title,
                body=request.body or "",
                labels=request.labels,
            )
        )
        return _to_issue(issue)


class GithubCommentIssue(_GitHubTool):
    name = "github.comment_issue"
    description = (
        "Add a comment to an existing issue. Synonyms: reply, respond, post a "
        "note, follow up on a ticket."
    )
    Input = GithubCommentIssueRequest
    Output = GithubComment

    def run(self, request: GithubCommentIssueRequest) -> GithubComment:
        comment = self._call(
            lambda: self._repo().get_issue(request.number).create_comment(request.body)
        )
        return GithubComment(id=comment.id, url=comment.html_url)


class GithubGetPr(_GitHubTool):
    name = "github.get_pr"
    description = (
        "Fetch a single pull request by number, with its changed files. Synonyms: "
        "read PR, show pull request, inspect merge request, get change."
    )
    Input = GithubGetPrRequest
    Output = PullRequest

    def run(self, request: GithubGetPrRequest) -> PullRequest:
        def operation():
            pull = self._repo().get_pull(request.number)
            return pull, list(pull.get_files())

        pull, files = self._call(operation)
        changed = [changed.filename for changed in files]
        return _to_pull(pull, changed_files=changed, diff=_diff_from_files(files))


class GithubListPrs(_GitHubTool):
    name = "github.list_prs"
    description = (
        "List pull requests, optionally filtered by state. Synonyms: enumerate "
        "PRs, browse merge requests, open pull requests, find changes."
    )
    Input = GithubListPrsRequest
    Output = GithubPrList

    def run(self, request: GithubListPrsRequest) -> GithubPrList:
        pulls = self._call(lambda: list(self._repo().get_pulls(state=request.state)))
        return GithubPrList(
            pull_requests=[_to_pull(pull, changed_files=[]) for pull in pulls]
        )


class GithubCreatePr(_GitHubTool):
    name = "github.create_pr"
    description = (
        "Open a pull request from a head branch into a base branch. Synonyms: "
        "raise a PR, propose a change, submit merge request, open pull request."
    )
    Input = GithubCreatePrRequest
    Output = PullRequest

    def run(self, request: GithubCreatePrRequest) -> PullRequest:
        try:
            pull = self._call(
                lambda: self._repo().create_pull(
                    title=request.title,
                    head=request.head_ref,
                    base=request.base_ref,
                    body=request.body or "",
                )
            )
        except MalformedInputError as rejected:
            # GitHub answers an unpushed or zero-commit head branch with a 422,
            # which _classify folds to MalformedInputError. The bare "request
            # rejected" reading made the agent think its *inputs* were wrong and
            # abandon the PR; name the head branch and the remedy instead, so it
            # self-corrects — push a branch a commit ahead of the base, then retry.
            raise MalformedInputError(
                f"{rejected} — the head branch '{request.head_ref}' must be pushed "
                f"to the remote and be at least one commit ahead of "
                f"'{request.base_ref}'. Commit your work on '{request.head_ref}', "
                "push it (git.push), then open the pull request again."
            ) from rejected
        return _to_pull(pull, changed_files=[])


class GithubReviewPr(_GitHubTool):
    name = "github.review_pr"
    description = (
        "Submit a review on a pull request from a reviewer's verdict (approve or "
        "comment). Synonyms: approve PR, leave a review, sign off, assess change."
    )
    Input = GithubReviewPrRequest
    Output = GithubReviewSubmission

    def run(self, request: GithubReviewPrRequest) -> GithubReviewSubmission:
        event = _REVIEW_EVENTS.get(request.review.verdict, "COMMENT")
        body = request.review.summary or ""
        review = self._call(
            lambda: self._repo().get_pull(request.number).create_review(body=body, event=event)
        )
        return GithubReviewSubmission(submitted=True, review_id=review.id)


class GithubRequestChanges(_GitHubTool):
    name = "github.request_changes"
    description = (
        "Submit a changes-requested review on a pull request. Synonyms: block PR, "
        "request edits, reject change, ask for fixes."
    )
    Input = GithubRequestChangesRequest
    Output = GithubReviewSubmission

    def run(self, request: GithubRequestChangesRequest) -> GithubReviewSubmission:
        review = self._call(
            lambda: self._repo()
            .get_pull(request.number)
            .create_review(body=request.body, event="REQUEST_CHANGES")
        )
        return GithubReviewSubmission(submitted=True, review_id=review.id)


class GithubMergePr(_GitHubTool):
    name = "github.merge_pr"
    description = (
        "Merge a pull request using a merge method (merge, squash, rebase). "
        "Synonyms: land PR, integrate change, close and merge, ship pull request."
    )
    Input = GithubMergePrRequest
    Output = GithubMergeResult

    def run(self, request: GithubMergePrRequest) -> GithubMergeResult:
        status = self._call(
            lambda: self._repo().get_pull(request.number).merge(merge_method=request.method)
        )
        return GithubMergeResult(merged=status.merged, sha=status.sha)


class GithubListChecks(_GitHubTool):
    name = "github.list_checks"
    description = (
        "List the CI check runs for a ref. Synonyms: build status, test results, "
        "CI checks, did the pipeline pass."
    )
    Input = GithubListChecksRequest
    Output = GithubCheckList

    def run(self, request: GithubListChecksRequest) -> GithubCheckList:
        runs = self._call(
            lambda: list(self._repo().get_commit(request.ref).get_check_runs())
        )
        return GithubCheckList(
            checks=[
                CheckRun(name=run.name, status=run.status, conclusion=run.conclusion)
                for run in runs
            ]
        )


class GithubGetCheckLogs(_GitHubTool):
    name = "github.get_check_logs"
    description = (
        "Fetch the logs for a single failing check run. Synonyms: build logs, CI "
        "output, why did the check fail, failure details."
    )
    Input = GithubGetCheckLogsRequest
    Output = GithubCheckLogs

    def run(self, request: GithubGetCheckLogsRequest) -> GithubCheckLogs:
        run = self._call(lambda: self._repo().get_check_run(request.check_id))
        output = getattr(run, "output", None)
        logs = getattr(output, "text", None) or getattr(output, "summary", None) or ""
        return GithubCheckLogs(name=run.name, logs=logs)


class GithubGetFileAtRef(_GitHubTool):
    name = "github.get_file_at_ref"
    description = (
        "Read a file's contents at a specific ref without a checkout. Synonyms: "
        "show file at commit, cat at branch, fetch file contents, read at ref."
    )
    Input = GithubGetFileAtRefRequest
    Output = GithubFileContent

    def run(self, request: GithubGetFileAtRefRequest) -> GithubFileContent:
        content = self._call(
            lambda: self._repo().get_contents(request.path, ref=request.ref)
        )
        return GithubFileContent(
            path=request.path,
            ref=request.ref,
            content=content.decoded_content.decode("utf-8", errors="replace"),
        )


class GithubSearchCode(_GitHubTool):
    name = "github.search_code"
    description = (
        "Search code across the repository for a query. Synonyms: grep remotely, "
        "find in code, locate usages, code search."
    )
    Input = GithubSearchCodeRequest
    Output = GithubCodeSearchResult

    def run(self, request: GithubSearchCodeRequest) -> GithubCodeSearchResult:
        def operation():
            session = self._active()
            query = f"{request.query} repo:{session.slug}"
            return list(session.client().search_code(query))[: request.max_results]

        results = self._call(operation)
        return GithubCodeSearchResult(matches=[_to_match(result) for result in results])


def _to_match(result: object) -> CodeMatch:
    """Project a code-search hit onto the typed :class:`CodeMatch` contract."""
    snippet = ""
    try:
        snippet = result.decoded_content.decode("utf-8", errors="replace").splitlines()[0]
    except (AttributeError, IndexError, UnicodeError):
        snippet = ""
    return CodeMatch(file=result.path, line=1, snippet=snippet)


# ---- construction: bind the fourteen tools to a session ---------------------
_TOOL_CLASSES: tuple[type[_GitHubTool], ...] = (
    GithubGetIssue,
    GithubListIssues,
    GithubCreateIssue,
    GithubCommentIssue,
    GithubGetPr,
    GithubListPrs,
    GithubCreatePr,
    GithubReviewPr,
    GithubRequestChanges,
    GithubMergePr,
    GithubListChecks,
    GithubGetCheckLogs,
    GithubGetFileAtRef,
    GithubSearchCode,
)


def bind_session(session: GitHubSession | None) -> dict[str, _GitHubTool]:
    """Construct the fourteen tools bound to ``session`` (``None`` = lazy default)."""
    return {cls.name: cls(session) for cls in _TOOL_CLASSES}


# The registered singletons use the lazy default session (built on first call),
# so importing this module never touches the network or needs a token.
TOOLS = tuple(bind_session(None).values())

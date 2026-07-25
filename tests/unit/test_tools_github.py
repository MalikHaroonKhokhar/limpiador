"""Unit tests for the ``github.*`` namespace — remote collaboration (HAR-18).

This is the second real namespace (property #1, 2/5) and the one that exercises
the resilience layer hardest: every one of the fourteen tools routes its API
call through the single :class:`GitHubBoundary` (HAR-16), so a rate-limit answer
is backed off and retried, and a persistent one gives up as a typed
``TransientError`` rather than a raw ``RateLimitExceededException``. A
non-transient failure the agent must read — a 404, a 403 — is translated by the
tool into the matching typed ``ToolError``.

The tests drive the tools with a *fake* GitHub client (no network): the client
and the resilience clock are injected through a :class:`GitHubSession`, so the
backoff and the token bucket run on a fake clock with no real waiting. Each tool
is held to returning the right typed object on the happy path, mapping
not-found/permission failures to the right typed error, and being reachable
through the registry the same generic way every other tool is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from github import GithubException, RateLimitExceededException

from limpiador.observability.errors import (
    MalformedInputError,
    NotFoundError,
    PermissionDeniedError,
    ToolError,
    TransientError,
)
from limpiador.observability.retry import RateLimit, Resilience, RetryPolicy
from limpiador.schemas import (
    GithubCheckList,
    GithubCheckLogs,
    GithubCodeSearchResult,
    GithubComment,
    GithubFileContent,
    GithubIssueList,
    GithubMergeResult,
    GithubPrList,
    GithubReviewSubmission,
    Issue,
    PullRequest,
    ReviewResult,
    Verdict,
)
from limpiador.tools import github_tools
from limpiador.tools.github_client import GitHubBoundary
from limpiador.tools.github_tools import GitHubSession
from limpiador.tools.registry import CORE_TOOL_NAMES, ToolRegistry

_GITHUB_TOOL_NAMES = (
    "github.get_issue",
    "github.list_issues",
    "github.create_issue",
    "github.comment_issue",
    "github.get_pr",
    "github.list_prs",
    "github.create_pr",
    "github.review_pr",
    "github.request_changes",
    "github.merge_pr",
    "github.list_checks",
    "github.get_check_logs",
    "github.get_file_at_ref",
    "github.search_code",
)


# ---- fakes: a GitHub client surface with no network -------------------------
def _ns(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _returns(value: object):
    return lambda *args, **kwargs: value


def _raises(error: Exception):
    def method(*args: object, **kwargs: object):
        raise error

    return method


def _flaky(errors: list[Exception], value: object):
    """A method that raises each queued error in turn, then returns ``value``."""
    queue = list(errors)

    def method(*args: object, **kwargs: object):
        if queue:
            raise queue.pop(0)
        return value

    return method


def _issue(number: int = 1, title: str = "Fix the flaky test", state: str = "open"):
    return _ns(
        number=number,
        title=title,
        state=state,
        body="some details",
        user=_ns(login="octocat"),
        labels=[_ns(name="bug")],
        create_comment=_returns(_ns(id=99, html_url="https://gh/c/99")),
    )


def _pull(number: int = 1, title: str = "Add a feature", state: str = "open"):
    return _ns(
        number=number,
        title=title,
        state=state,
        head=_ns(ref="feature"),
        base=_ns(ref="main"),
        body="why this change",
        get_files=_returns([
            _ns(filename="src/a.py", patch="@@ -1 +1 @@\n-old a\n+new a"),
            _ns(filename="src/b.py", patch="@@ -1 +1 @@\n-old b\n+new b"),
        ]),
        create_review=_returns(_ns(id=7)),
        merge=_returns(_ns(merged=True, sha="deadbee")),
    )


def _check():
    return _ns(name="build", status="completed", conclusion="success")


def _happy_repo() -> SimpleNamespace:
    return _ns(
        get_issue=_returns(_issue()),
        get_issues=_returns([_issue(1), _issue(2, title="A second issue")]),
        create_issue=_returns(_issue(3, title="A created issue")),
        get_pull=_returns(_pull()),
        get_pulls=_returns([_pull(1), _pull(2, title="A second PR")]),
        create_pull=_returns(_pull(4, title="An opened PR")),
        get_commit=_returns(_ns(get_check_runs=_returns([_check()]))),
        get_check_run=_returns(_ns(name="build", output=_ns(text="the build logs"))),
        get_contents=_returns(_ns(path="src/a.py", decoded_content=b"print('hi')\n")),
    )


def _happy_client(repo: SimpleNamespace) -> SimpleNamespace:
    return _ns(
        get_repo=_returns(repo),
        search_code=_returns([_ns(path="src/a.py", decoded_content=b"x = 1\ny = 2\n")]),
    )


def _bind(
    *,
    repo: SimpleNamespace | None = None,
    client: SimpleNamespace | None = None,
    max_attempts: int = 4,
    rate: float = 1000.0,
    burst: int = 1000,
):
    """Build the fourteen tools bound to a fake session on a fake clock."""
    clock = _FakeClock()
    repo = repo if repo is not None else _happy_repo()
    client = client if client is not None else _happy_client(repo)
    boundary = GitHubBoundary(
        resilience=Resilience(
            retry=RetryPolicy(max_attempts=max_attempts, base_delay_s=0.5),
            rate_limit=RateLimit(rate_per_second=rate, burst=burst),
            sleep=clock.sleep,
            clock=clock.time,
        )
    )
    session = GitHubSession(
        boundary=boundary,
        client_factory=lambda: client,
        slug="octocat/limpiador",
    )
    return github_tools.bind_session(session), repo, client, clock


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---- happy paths: each tool returns the right typed object -------------------
def test_get_issue_returns_typed_issue() -> None:
    tools, *_ = _bind()
    result = tools["github.get_issue"].invoke({"number": 1})
    assert isinstance(result, Issue)
    assert result.number == 1
    assert result.author == "octocat"
    assert result.labels == ["bug"]


def test_list_issues_returns_typed_list() -> None:
    tools, *_ = _bind()
    result = tools["github.list_issues"].invoke({"state": "open"})
    assert isinstance(result, GithubIssueList)
    assert [issue.number for issue in result.issues] == [1, 2]


def test_create_issue_forwards_fields_and_returns_typed_issue() -> None:
    captured: dict[str, object] = {}

    def create_issue(**kwargs: object):
        captured.update(kwargs)
        return _issue(5, title=str(kwargs["title"]))

    repo = _happy_repo()
    repo.create_issue = create_issue
    tools, *_ = _bind(repo=repo)

    result = tools["github.create_issue"].invoke(
        {"title": "A new bug", "body": "what is wrong", "labels": ["bug"]}
    )
    assert isinstance(result, Issue)
    assert result.title == "A new bug"
    assert captured["title"] == "A new bug"


def test_comment_issue_returns_typed_comment() -> None:
    tools, *_ = _bind()
    result = tools["github.comment_issue"].invoke({"number": 1, "body": "thanks!"})
    assert isinstance(result, GithubComment)
    assert result.id == 99


def test_get_pr_returns_typed_pull_request() -> None:
    tools, *_ = _bind()
    result = tools["github.get_pr"].invoke({"number": 1})
    assert isinstance(result, PullRequest)
    assert result.head_ref == "feature"
    assert result.base_ref == "main"
    assert result.changed_files == ["src/a.py", "src/b.py"]
    # The diff is assembled from the per-file patches — the typed input that
    # feeds spawn_reviewer (the get_pr -> reviewer composition chain).
    assert result.diff is not None
    assert "new a" in result.diff and "new b" in result.diff


def test_list_prs_returns_typed_list() -> None:
    tools, *_ = _bind()
    result = tools["github.list_prs"].invoke({"state": "open"})
    assert isinstance(result, GithubPrList)
    assert [pr.number for pr in result.pull_requests] == [1, 2]


def test_create_pr_returns_typed_pull_request() -> None:
    tools, *_ = _bind()
    result = tools["github.create_pr"].invoke(
        {"title": "My change", "head_ref": "feature", "base_ref": "main"}
    )
    assert isinstance(result, PullRequest)
    assert result.title == "An opened PR"


def test_review_pr_submits_and_returns_submission() -> None:
    captured: dict[str, object] = {}

    def create_review(**kwargs: object):
        captured.update(kwargs)
        return _ns(id=7)

    pull = _pull()
    pull.create_review = create_review
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    review = ReviewResult(verdict=Verdict.APPROVE, summary="LGTM")
    result = tools["github.review_pr"].invoke({"number": 1, "review": review.model_dump()})
    assert isinstance(result, GithubReviewSubmission)
    assert result.submitted is True
    assert result.review_id == 7
    assert captured["event"] == "APPROVE"


def test_review_pr_accepts_the_natural_severity_words_a_model_emits() -> None:
    """Replays a real captured failure: a review call died with MalformedInputError
    because a finding's severity was 'high'/'medium' — words the Severity enum did
    not accept — so the whole github.review_pr call was thrown away. The raw payload
    a reviewing model emits must now validate and submit the changes-request."""
    captured: dict[str, object] = {}

    def create_review(**kwargs: object):
        captured.update(kwargs)
        return _ns(id=11)

    pull = _pull()
    pull.create_review = create_review
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    raw_review = {
        "number": 1,
        "review": {
            "verdict": "request_changes",
            "findings": [
                {
                    "severity": "high",
                    "file": "utils.py",
                    "line": 4,
                    "message": "mutable default argument",
                    "suggestion": "use None and initialize inside the function",
                },
                {"severity": "medium", "file": "net.py", "line": 9, "message": "unreachable code"},
            ],
            "summary": "two issues to address",
        },
    }

    result = tools["github.review_pr"].invoke(raw_review)

    assert isinstance(result, GithubReviewSubmission)
    assert result.submitted is True
    assert captured["event"] == "REQUEST_CHANGES"


def test_request_changes_submits_a_request_changes_review() -> None:
    captured: dict[str, object] = {}

    def create_review(**kwargs: object):
        captured.update(kwargs)
        return _ns(id=8)

    pull = _pull()
    pull.create_review = create_review
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    result = tools["github.request_changes"].invoke({"number": 1, "body": "please fix"})
    assert isinstance(result, GithubReviewSubmission)
    assert captured["event"] == "REQUEST_CHANGES"


def _own_pr_review(events: list[str]):
    """A create_review that mimics GitHub: it 422s on approve/request-changes for
    your own PR (recording the attempt) but accepts a COMMENT review."""

    def create_review(**kwargs: object):
        event = str(kwargs["event"])
        events.append(event)
        if event in ("REQUEST_CHANGES", "APPROVE"):
            raise GithubException(
                422,
                {
                    "message": "Unprocessable Entity",
                    "errors": ["Review Can not request changes on your own pull request"],
                },
                None,
            )
        return _ns(id=77)

    return create_review


def test_request_changes_on_your_own_pr_degrades_to_a_comment_review() -> None:
    """GitHub forbids requesting changes on your own PR, but the findings still
    matter and a COMMENT review is the one reviewing action it allows. The tool
    posts them as a comment instead of losing the review to the rule, and says so."""
    events: list[str] = []
    pull = _pull()
    pull.create_review = _own_pr_review(events)
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    result = tools["github.request_changes"].invoke({"number": 13, "body": "mutable default arg"})

    assert isinstance(result, GithubReviewSubmission)
    assert result.submitted is True
    assert result.downgraded_to == "comment"
    assert "own pull request" in (result.note or "").lower()
    assert events == ["REQUEST_CHANGES", "COMMENT"]  # tried the rule, then degraded


def test_review_pr_approve_on_your_own_pr_degrades_to_a_comment_review() -> None:
    """The same rule blocks approving your own PR; review_pr degrades identically."""
    events: list[str] = []
    pull = _pull()
    pull.create_review = _own_pr_review(events)
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    review = ReviewResult(verdict=Verdict.APPROVE, summary="LGTM")
    result = tools["github.review_pr"].invoke({"number": 13, "review": review.model_dump()})

    assert result.downgraded_to == "comment"
    assert events == ["APPROVE", "COMMENT"]


def test_a_generic_422_still_raises_rather_than_degrading() -> None:
    """The degrade is specific to the own-PR rule. A different 422 (a genuinely
    unprocessable request) must still surface as a typed error, not be silently
    turned into a comment."""
    events: list[str] = []

    def create_review(**kwargs: object):
        events.append(str(kwargs["event"]))
        raise GithubException(422, {"message": "Validation Failed"}, None)

    pull = _pull()
    pull.create_review = create_review
    repo = _happy_repo()
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    with pytest.raises(ToolError):
        tools["github.request_changes"].invoke({"number": 1, "body": "please fix"})
    assert events == ["REQUEST_CHANGES"]  # no COMMENT fallback for an unrelated 422


def test_merge_pr_returns_typed_merge_result() -> None:
    tools, *_ = _bind()
    result = tools["github.merge_pr"].invoke({"number": 1, "method": "squash"})
    assert isinstance(result, GithubMergeResult)
    assert result.merged is True
    assert result.sha == "deadbee"


def test_list_checks_returns_typed_check_list() -> None:
    tools, *_ = _bind()
    result = tools["github.list_checks"].invoke({"ref": "abc123"})
    assert isinstance(result, GithubCheckList)
    assert result.checks[0].name == "build"
    assert result.checks[0].conclusion == "success"


def test_get_check_logs_returns_typed_logs() -> None:
    tools, *_ = _bind()
    result = tools["github.get_check_logs"].invoke({"check_id": 42})
    assert isinstance(result, GithubCheckLogs)
    assert result.name == "build"
    assert "build logs" in result.logs


def test_get_file_at_ref_decodes_content() -> None:
    tools, *_ = _bind()
    result = tools["github.get_file_at_ref"].invoke({"path": "src/a.py", "ref": "main"})
    assert isinstance(result, GithubFileContent)
    assert result.content == "print('hi')\n"
    assert result.ref == "main"


def test_search_code_returns_typed_matches() -> None:
    tools, *_ = _bind()
    result = tools["github.search_code"].invoke({"query": "TODO"})
    assert isinstance(result, GithubCodeSearchResult)
    assert result.matches[0].file == "src/a.py"


# ---- resilience: 429 backoff + limiter on a fake clock, then typed give-up ---
def test_a_rate_limit_is_backed_off_and_retried_then_succeeds() -> None:
    repo = _happy_repo()
    repo.get_issue = _flaky(
        [RateLimitExceededException(403, {"message": "rate limit"}, None)],
        _issue(1),
    )
    tools, _repo, _client, clock = _bind(repo=repo, max_attempts=4)

    result = tools["github.get_issue"].invoke({"number": 1})

    assert isinstance(result, Issue)
    assert clock.now > 0.0  # the backoff slept on the fake clock before retrying


def test_persistent_rate_limits_give_up_with_a_typed_transient_error() -> None:
    repo = _happy_repo()
    repo.get_issue = _raises(RateLimitExceededException(429, {"message": "rate limit"}, None))
    tools, *_ = _bind(repo=repo, max_attempts=3)

    with pytest.raises(TransientError):
        tools["github.get_issue"].invoke({"number": 1})


def test_the_boundary_meters_tool_calls_through_its_limiter() -> None:
    tools, _repo, _client, clock = _bind(rate=10.0, burst=1)

    tools["github.get_issue"].invoke({"number": 1})  # the single burst token
    tools["github.get_issue"].invoke({"number": 1})  # throttled to 10/s -> 0.1s

    assert abs(clock.now - 0.1) < 1e-9


# ---- failure mapping: not-found / permission -> the right typed error --------
def test_a_404_maps_to_not_found_error() -> None:
    repo = _happy_repo()
    repo.get_issue = _raises(GithubException(404, {"message": "Not Found"}, None))
    tools, *_ = _bind(repo=repo)

    with pytest.raises(NotFoundError):
        tools["github.get_issue"].invoke({"number": 404})


def test_a_403_maps_to_permission_denied_error() -> None:
    repo = _happy_repo()
    repo.create_issue = _raises(GithubException(403, {"message": "Forbidden"}, None))
    tools, *_ = _bind(repo=repo)

    with pytest.raises(PermissionDeniedError):
        tools["github.create_issue"].invoke({"title": "blocked"})


def test_create_pr_422_tells_the_agent_to_push_a_branch_with_a_commit() -> None:
    # GitHub rejects an unpushed or empty head branch with a 422. The bare
    # "request rejected (422)" reading led the agent to think its *inputs* were
    # malformed and give up; the message must instead name the head branch and
    # tell it to push a branch that is a commit ahead of the base, so it can
    # self-correct (git.push, then retry create_pr) rather than abandon the PR.
    repo = _happy_repo()
    repo.create_pull = _raises(
        GithubException(
            422,
            {
                "message": "Validation Failed",
                "errors": [{"message": "No commits between main and feature"}],
            },
            None,
        )
    )
    tools, *_ = _bind(repo=repo)

    with pytest.raises(MalformedInputError) as caught:
        tools["github.create_pr"].invoke(
            {"title": "My change", "head_ref": "feature", "base_ref": "main"}
        )
    message = str(caught.value).lower()
    assert "feature" in message  # it names the head branch that is not ready
    assert "push" in message  # and tells the agent how to recover
    assert "commit" in message


def test_a_422_review_on_your_own_pr_surfaces_githubs_actual_reason() -> None:
    """GitHub forbids requesting changes on your own PR and answers 422 with the
    reason in ``errors[]`` (a real captured run: 'Review Can not request changes on
    your own pull request'). The folded error must carry that reason, not just the
    bare top-level 'Unprocessable Entity' — otherwise the agent cannot tell a rule
    violation from a malformed body, decides its *format* was wrong, and gives up."""
    repo = _happy_repo()
    pull = _pull()
    pull.create_review = _raises(
        GithubException(
            422,
            {
                "message": "Unprocessable Entity",
                "errors": ["Review Can not request changes on your own pull request"],
            },
            None,
        )
    )
    repo.get_pull = _returns(pull)
    tools, *_ = _bind(repo=repo)

    with pytest.raises(ToolError) as caught:
        tools["github.request_changes"].invoke({"number": 13, "body": "please fix"})

    assert "own pull request" in str(caught.value).lower()


def test_every_mapped_failure_is_a_recoverable_tool_error() -> None:
    repo = _happy_repo()
    repo.get_pull = _raises(GithubException(404, {"message": "Not Found"}, None))
    tools, *_ = _bind(repo=repo)

    with pytest.raises(ToolError):
        tools["github.get_pr"].invoke({"number": 7})


# ---- registry: all fourteen searchable + loadable, none in the core ---------
def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in github_tools.TOOLS:
        registry.register(tool)
    return registry


def test_namespace_exposes_exactly_the_fourteen_tools() -> None:
    assert tuple(tool.name for tool in github_tools.TOOLS) == _GITHUB_TOOL_NAMES


def test_every_github_tool_is_loadable_and_none_are_core() -> None:
    registry = _fresh_registry()
    for name in _GITHUB_TOOL_NAMES:
        assert name not in CORE_TOOL_NAMES
        assert registry.load({"name": name}).loaded is True
    assert set(registry.loaded_names()) == set(_GITHUB_TOOL_NAMES)


def test_every_github_tool_is_searchable() -> None:
    registry = _fresh_registry()
    for name in _GITHUB_TOOL_NAMES:
        verb = name.split(".", 1)[1].replace("_", " ")
        found = registry.search({"query": verb, "limit": 56}).summaries
        assert name in {summary.name for summary in found}

"""``github.*`` namespace — remote collaboration (ARCHITECTURE.md §5.3, 14 tools).

get_issue, list_issues, create_issue, comment_issue, get_pr, list_prs,
create_pr, review_pr, request_changes, merge_pr, list_checks, get_check_logs,
get_file_at_ref, search_code. Backed by the real GitHub API (pygithub). Every
executor routes its API call through the one resilient boundary in
``github_client.GitHubBoundary`` — the retry/backoff and rate limiter applied in
a single place (observability §13), not scattered across these fourteen tools.
"""

from __future__ import annotations

from limpiador.schemas import (
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
)
from limpiador.tools.base import declared_tool

TOOLS = (
    declared_tool("github.get_issue", "Fetch a single issue by number.", GithubGetIssueRequest, Issue),
    declared_tool("github.list_issues", "List issues, optionally filtered by state or label.", GithubListIssuesRequest, GithubIssueList),
    declared_tool("github.create_issue", "Open a new issue with a title and body.", GithubCreateIssueRequest, Issue),
    declared_tool("github.comment_issue", "Add a comment to an existing issue.", GithubCommentIssueRequest, GithubComment),
    declared_tool("github.get_pr", "Fetch a single pull request by number.", GithubGetPrRequest, PullRequest),
    declared_tool("github.list_prs", "List pull requests, optionally filtered by state.", GithubListPrsRequest, GithubPrList),
    declared_tool("github.create_pr", "Open a pull request from a head branch into a base branch.", GithubCreatePrRequest, PullRequest),
    declared_tool("github.review_pr", "Submit an approving or commenting review on a pull request.", GithubReviewPrRequest, GithubReviewSubmission),
    declared_tool("github.request_changes", "Submit a changes-requested review on a pull request.", GithubRequestChangesRequest, GithubReviewSubmission),
    declared_tool("github.merge_pr", "Merge a pull request using the given merge method.", GithubMergePrRequest, GithubMergeResult),
    declared_tool("github.list_checks", "List the CI check runs for a ref.", GithubListChecksRequest, GithubCheckList),
    declared_tool("github.get_check_logs", "Fetch the logs for a single failing check run.", GithubGetCheckLogsRequest, GithubCheckLogs),
    declared_tool("github.get_file_at_ref", "Read a file's contents at a specific ref without a checkout.", GithubGetFileAtRefRequest, GithubFileContent),
    declared_tool("github.search_code", "Search code across the repository for a query.", GithubSearchCodeRequest, GithubCodeSearchResult),
)

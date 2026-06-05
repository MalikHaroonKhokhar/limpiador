"""``github.*`` namespace — remote collaboration (ARCHITECTURE.md §5.3, 14 tools).

get_issue, list_issues, create_issue, comment_issue, get_pr, list_prs,
create_pr, review_pr, request_changes, merge_pr, list_checks, get_check_logs,
get_file_at_ref, search_code. Backed by the real GitHub API (pygithub); external
calls go through the retry/backoff and rate limiter (observability §13).
"""

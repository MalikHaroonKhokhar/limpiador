"""``git.*`` namespace — local repository state (ARCHITECTURE.md §5.3, 12 tools).

status, diff, log, show, branch_list, branch_create, checkout, stage, commit,
reset, stash, blame. Backed by real git (gitpython); each tool emits a typed
object from :mod:`limpiador.schemas` and raises a typed ``ToolError`` on failure.
"""

from __future__ import annotations

from limpiador.schemas import (
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
from limpiador.tools.base import declared_tool

TOOLS = (
    declared_tool("git.status", "Show the working-tree status of the repository.", GitStatusRequest, GitStatusResult),
    declared_tool("git.diff", "Show repository diffs for a ref, path, or staged changes.", GitDiffRequest, GitDiffResult),
    declared_tool("git.log", "List recent commits, optionally limited to a path.", GitLogRequest, GitLogResult),
    declared_tool("git.show", "Show one commit or ref, including commit metadata and diff.", GitShowRequest, GitShowResult),
    declared_tool("git.branch_list", "List local branches and identify the current branch.", GitBranchListRequest, GitBranchListResult),
    declared_tool("git.branch_create", "Create a local branch from an optional base ref.", GitBranchCreateRequest, GitBranchCreateResult),
    declared_tool("git.checkout", "Check out an existing ref or create and check out a branch.", GitCheckoutRequest, GitCheckoutResult),
    declared_tool("git.stage", "Stage one or more paths for commit.", GitStageRequest, GitStageResult),
    declared_tool("git.commit", "Commit staged changes with a message and return the new commit.", GitCommitRequest, GitCommitResult),
    declared_tool("git.reset", "Reset the repository to a ref, optionally hard.", GitResetRequest, GitResetResult),
    declared_tool("git.stash", "Create or pop a git stash entry.", GitStashRequest, GitStashResult),
    declared_tool("git.blame", "Blame a file or line range to identify responsible commits.", GitBlameRequest, GitBlameResult),
)

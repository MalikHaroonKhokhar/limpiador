"""``git.*`` namespace — local repository state (ARCHITECTURE.md §5.3, 12 tools).

status, diff, log, show, branch_list, branch_create, checkout, stage, commit,
reset, stash, blame. Backed by real git (gitpython); each tool emits a typed
object from :mod:`limpiador.schemas` and raises a typed ``ToolError`` on failure.
"""

"""``fs.*`` namespace — filesystem (ARCHITECTURE.md §5.3, 10 tools).

read_file, write_file, list_dir, glob, grep, move, delete, mkdir, file_stat,
apply_patch. The read-only subset (read_file, list_dir, glob, grep, file_stat)
is what the reviewer subagent is scoped to; the writers are excluded from that
scope by construction (§9).
"""

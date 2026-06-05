"""``fs.*`` namespace — filesystem (ARCHITECTURE.md §5.3, 10 tools).

read_file, write_file, list_dir, glob, grep, move, delete, mkdir, file_stat,
apply_patch. The read-only subset (read_file, list_dir, glob, grep, file_stat)
is what the reviewer subagent is scoped to; the writers are excluded from that
scope by construction (§9).
"""

from __future__ import annotations

from limpiador.schemas import (
    FsApplyPatchRequest,
    FsApplyPatchResult,
    FsDeleteRequest,
    FsDeleteResult,
    FsDirListing,
    FsFileContent,
    FsFileStat,
    FsFileStatRequest,
    FsGlobRequest,
    FsGlobResult,
    FsGrepRequest,
    FsGrepResult,
    FsListDirRequest,
    FsMkdirRequest,
    FsMkdirResult,
    FsMoveRequest,
    FsMoveResult,
    FsReadFileRequest,
    FsWriteFileRequest,
    FsWriteResult,
)
from limpiador.tools.base import declared_tool

TOOLS = (
    declared_tool("fs.read_file", "Read a text file's contents from the working tree.", FsReadFileRequest, FsFileContent),
    declared_tool("fs.write_file", "Write text contents to a file, creating or overwriting it.", FsWriteFileRequest, FsWriteResult),
    declared_tool("fs.list_dir", "List the entries of a directory.", FsListDirRequest, FsDirListing),
    declared_tool("fs.glob", "Find files matching a glob pattern.", FsGlobRequest, FsGlobResult),
    declared_tool("fs.grep", "Search file contents for a pattern and return matching lines.", FsGrepRequest, FsGrepResult),
    declared_tool("fs.move", "Move or rename a file or directory.", FsMoveRequest, FsMoveResult),
    declared_tool("fs.delete", "Delete a file or directory.", FsDeleteRequest, FsDeleteResult),
    declared_tool("fs.mkdir", "Create a directory, including any missing parents.", FsMkdirRequest, FsMkdirResult),
    declared_tool("fs.file_stat", "Stat a path for size, type, and modification time.", FsFileStatRequest, FsFileStat),
    declared_tool("fs.apply_patch", "Apply a unified-diff patch to the working tree.", FsApplyPatchRequest, FsApplyPatchResult),
)

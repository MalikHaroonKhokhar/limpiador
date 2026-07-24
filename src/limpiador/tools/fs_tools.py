"""``fs.*`` namespace — scoped filesystem access (ARCHITECTURE.md §5.3, 10 tools).

read_file, write_file, list_dir, glob, grep, move, delete, mkdir, file_stat,
apply_patch. Two properties shape this namespace beyond "read and write":

* **Scoped outputs.** A tool returns the *smallest* thing that answers the
  question — ``read_file`` honors a line range, ``grep`` returns the matching
  *lines* (file, number, text), never the whole file dumped into context.
* **No escaping the root.** Every path is resolved against the repository root
  (the ``--repo`` the CLI anchors the agent to, resolved ambiently from the
  working directory). A ``../../etc`` or absolute path that would leave the
  root is a typed :class:`PermissionDeniedError`, not a silent read of the host.

``apply_patch`` is *atomic*: the new content for every file in the patch is
computed in memory first, and only if all hunks apply cleanly is anything
written — one bad hunk leaves the working tree untouched.

The read-only subset (read_file, list_dir, glob, grep, file_stat) is what the
reviewer subagent is scoped to; the writers are excluded from that scope by
construction (§9).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from limpiador.observability.errors import (
    MalformedInputError,
    NotFoundError,
    PermissionDeniedError,
)
from limpiador.schemas import (
    DirEntry,
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
    GrepMatch,
)
from limpiador.tools.base import Tool


# ---- the root boundary: every path resolves inside it -----------------------
def _repo_root() -> Path:
    """The boundary the fs tools operate within — the working directory tree."""
    return Path.cwd().resolve()


def _safe_path(path: str) -> Path:
    """Resolve ``path`` inside the root, or raise if it would escape it."""
    root = _repo_root()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionDeniedError(
            f"path {path!r} escapes the repository root; access is denied."
        )
    return candidate


def _relativize(target: Path) -> str:
    """A path's location relative to the root, as a stable posix string."""
    return target.relative_to(_repo_root()).as_posix()


def _within_root(target: Path) -> bool:
    """Whether a (possibly symlinked) path still resolves inside the root."""
    root = _repo_root()
    resolved = target.resolve()
    return resolved == root or root in resolved.parents


# ---- read_file / write_file -------------------------------------------------
def _slice_lines(
    lines: list[str], start_line: int | None, end_line: int | None
) -> tuple[list[str], int]:
    """Keep only the inclusive ``[start, end]`` 1-based slice of a file's lines."""
    if start_line is None and end_line is None:
        return lines, 1
    start = start_line or 1
    end = end_line if end_line is not None else len(lines)
    return lines[start - 1 : end], start


class FsReadFile(Tool):
    name = "fs.read_file"
    description = (
        "Read a text file's contents, optionally just a line range to keep the "
        "result small. Synonyms: open, cat, show file, view, get contents, slice."
    )
    Input = FsReadFileRequest
    Output = FsFileContent

    def run(self, request: FsReadFileRequest) -> FsFileContent:
        target = _safe_path(request.path)
        if not target.is_file():
            raise NotFoundError(f"no file at {request.path!r}")
        lines = target.read_text().splitlines(keepends=True)
        sliced, start = _slice_lines(lines, request.start_line, request.end_line)
        return FsFileContent(
            path=request.path,
            content="".join(sliced),
            line_count=len(sliced),
            start_line=start,
        )


class FsWriteFile(Tool):
    name = "fs.write_file"
    description = (
        "Write text to a file, creating it and any missing parent directories or "
        "overwriting it. Synonyms: save, put, create file, replace contents."
    )
    Input = FsWriteFileRequest
    Output = FsWriteResult

    def run(self, request: FsWriteFileRequest) -> FsWriteResult:
        target = _safe_path(request.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = request.content.encode()
        target.write_bytes(data)
        return FsWriteResult(path=request.path, bytes_written=len(data))


# ---- list_dir / glob --------------------------------------------------------
class FsListDir(Tool):
    name = "fs.list_dir"
    description = (
        "List the entries of a directory and whether each is a directory. "
        "Synonyms: ls, dir, enumerate, what is in this folder, children."
    )
    Input = FsListDirRequest
    Output = FsDirListing

    def run(self, request: FsListDirRequest) -> FsDirListing:
        target = _safe_path(request.path)
        if not target.is_dir():
            raise NotFoundError(f"no directory at {request.path!r}")
        entries = [
            DirEntry(name=child.name, is_dir=child.is_dir())
            for child in sorted(target.iterdir(), key=lambda c: c.name)
        ]
        return FsDirListing(path=request.path, entries=entries)


class FsGlob(Tool):
    name = "fs.glob"
    description = (
        "Find files matching a glob pattern under a root directory. Synonyms: "
        "find, wildcard, match files, locate, *.py, search by name."
    )
    Input = FsGlobRequest
    Output = FsGlobResult

    def run(self, request: FsGlobRequest) -> FsGlobResult:
        root = _safe_path(request.root)
        if not root.is_dir():
            raise NotFoundError(f"no directory at {request.root!r}")
        matches = sorted(
            _relativize(match)
            for match in root.glob(request.pattern)
            if _within_root(match)
        )
        return FsGlobResult(matches=matches)


# ---- grep -------------------------------------------------------------------
def _compile(pattern: str, regex: bool) -> re.Pattern[str]:
    """Compile the search expression, folding a bad regex into a typed error."""
    expression = pattern if regex else re.escape(pattern)
    try:
        return re.compile(expression)
    except re.error as error:
        raise MalformedInputError(f"invalid grep pattern {pattern!r}: {error}") from error


def _grep_files(target: Path) -> list[Path]:
    """The files a grep should scan — the file itself, or a tree minus ``.git``."""
    if target.is_file():
        return [target]
    return [
        path
        for path in sorted(target.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    ]


def _grep_one(file: Path, matcher: re.Pattern[str]) -> list[GrepMatch]:
    """The matching lines of one file — never the whole file's body."""
    try:
        text = file.read_text()
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable file: nothing to match, skip it
    return [
        GrepMatch(file=_relativize(file), line=number, text=line)
        for number, line in enumerate(text.splitlines(), start=1)
        if matcher.search(line)
    ]


class FsGrep(Tool):
    name = "fs.grep"
    description = (
        "Search file contents for a pattern and return the matching lines (file, "
        "line number, text) — not whole files. Synonyms: search, find text, ripgrep."
    )
    Input = FsGrepRequest
    Output = FsGrepResult

    def run(self, request: FsGrepRequest) -> FsGrepResult:
        target = _safe_path(request.path)
        if not target.exists():
            raise NotFoundError(f"no file or directory at {request.path!r}")
        matcher = _compile(request.pattern, request.regex)
        matches: list[GrepMatch] = []
        for file in _grep_files(target):
            matches.extend(_grep_one(file, matcher))
        return FsGrepResult(matches=matches)


# ---- move / delete / mkdir / file_stat --------------------------------------
class FsMove(Tool):
    name = "fs.move"
    description = (
        "Move or rename a file or directory within the tree. Synonyms: rename, mv, "
        "relocate, change path."
    )
    Input = FsMoveRequest
    Output = FsMoveResult

    def run(self, request: FsMoveRequest) -> FsMoveResult:
        source = _safe_path(request.source)
        destination = _safe_path(request.destination)
        if not source.exists():
            raise NotFoundError(f"cannot move {request.source!r}: it does not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return FsMoveResult(source=request.source, destination=request.destination)


class FsDelete(Tool):
    name = "fs.delete"
    description = (
        "Delete a file, or a directory and everything under it. Synonyms: remove, "
        "rm, unlink, erase, trash."
    )
    Input = FsDeleteRequest
    Output = FsDeleteResult

    def run(self, request: FsDeleteRequest) -> FsDeleteResult:
        target = _safe_path(request.path)
        if not target.exists():
            raise NotFoundError(f"cannot delete {request.path!r}: it does not exist")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return FsDeleteResult(path=request.path, deleted=True)


class FsMkdir(Tool):
    name = "fs.mkdir"
    description = (
        "Create a directory, including any missing parents. Synonyms: make folder, "
        "mkdir -p, create directory, new folder."
    )
    Input = FsMkdirRequest
    Output = FsMkdirResult

    def run(self, request: FsMkdirRequest) -> FsMkdirResult:
        target = _safe_path(request.path)
        existed = target.exists()
        try:
            target.mkdir(parents=request.parents, exist_ok=True)
        except FileNotFoundError as error:
            raise NotFoundError(
                f"cannot create {request.path!r}: a parent directory is missing"
            ) from error
        return FsMkdirResult(path=request.path, created=not existed)


class FsStat(Tool):
    name = "fs.file_stat"
    description = (
        "Stat a path for size, type, and existence. Synonyms: stat, file info, "
        "size, exists, is it a directory, metadata."
    )
    Input = FsFileStatRequest
    Output = FsFileStat

    def run(self, request: FsFileStatRequest) -> FsFileStat:
        target = _safe_path(request.path)
        if not target.exists():
            return FsFileStat(path=request.path, size_bytes=0, is_dir=False, exists=False)
        return FsFileStat(
            path=request.path,
            size_bytes=target.stat().st_size,
            is_dir=target.is_dir(),
            exists=True,
        )


# ---- apply_patch: parse a unified diff, then apply it atomically ------------
@dataclass(frozen=True)
class _Hunk:
    old_start: int
    body: tuple[str, ...]


@dataclass(frozen=True)
class _FilePatch:
    path: str
    create: bool
    delete: bool
    hunks: tuple[_Hunk, ...]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def _strip_prefix(path: str) -> str:
    """Drop the conventional ``a/`` or ``b/`` diff prefix from a header path."""
    return path[2:] if path[:2] in ("a/", "b/") else path


def _parse_hunk(lines: list[str], index: int) -> tuple[_Hunk, int]:
    """Read one ``@@`` hunk and its body, returning it and the next line index."""
    match = _HUNK_RE.match(lines[index])
    if not match:
        raise MalformedInputError(f"malformed hunk header: {lines[index]!r}")
    body: list[str] = []
    index += 1
    while index < len(lines):
        entry = lines[index]
        if entry.startswith(("--- ", "@@")) or entry[:1] not in " +-":
            break
        body.append(entry)
        index += 1
    return _Hunk(old_start=int(match.group(1)), body=tuple(body)), index


def _parse_patch(text: str) -> list[_FilePatch]:
    """Parse a unified diff into per-file hunk groups (raises on a malformed one)."""
    lines = text.splitlines()
    patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old = lines[index][4:].strip()
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise MalformedInputError("malformed patch: a '---' line lacks its '+++'")
        new = lines[index + 1][4:].strip()
        index += 2
        hunks: list[_Hunk] = []
        while index < len(lines) and lines[index].startswith("@@"):
            hunk, index = _parse_hunk(lines, index)
            hunks.append(hunk)
        chosen = old if new == "/dev/null" else new
        patches.append(
            _FilePatch(
                path=_strip_prefix(chosen),
                create=old == "/dev/null",
                delete=new == "/dev/null",
                hunks=tuple(hunks),
            )
        )
    if not patches:
        raise MalformedInputError("no file sections found in the patch")
    return patches


def _apply_hunks(original: str, hunks: tuple[_Hunk, ...]) -> str:
    """Apply hunks to a file's text, raising if any context fails to match."""
    lines = original.splitlines()
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk.old_start - 1
        if start < cursor or start > len(lines):
            raise MalformedInputError("patch hunk does not align with the file")
        result.extend(lines[cursor:start])
        cursor = start
        for entry in hunk.body:
            tag, content = entry[:1], entry[1:]
            if tag in " -":
                if cursor >= len(lines) or lines[cursor] != content:
                    raise MalformedInputError("patch context does not match the file")
                cursor += 1
                if tag == " ":
                    result.append(content)
            else:  # '+': an added line
                result.append(content)
    result.extend(lines[cursor:])
    text = "\n".join(result)
    return text + "\n" if original.endswith("\n") else text


def _created_content(hunks: tuple[_Hunk, ...]) -> str:
    """The body of a newly created file — the added lines of its hunks."""
    added = [entry[1:] for hunk in hunks for entry in hunk.body if entry[:1] == "+"]
    text = "\n".join(added)
    return text + "\n" if text else text


def _plan_file(patch: _FilePatch) -> tuple[Path, str, str | None]:
    """Compute one file's outcome (target, relative path, new content or None=delete)."""
    target = _safe_path(patch.path)
    if patch.delete:
        if not target.exists():
            raise NotFoundError(f"cannot delete {patch.path!r}: it does not exist")
        return target, patch.path, None
    if patch.create:
        return target, patch.path, _created_content(patch.hunks)
    if not target.is_file():
        raise NotFoundError(f"cannot patch {patch.path!r}: it does not exist")
    return target, patch.path, _apply_hunks(target.read_text(), patch.hunks)


class FsApplyPatch(Tool):
    name = "fs.apply_patch"
    description = (
        "Apply a unified-diff patch to the working tree atomically — all hunks "
        "apply or nothing changes. Synonyms: patch, apply diff, edit via diff."
    )
    Input = FsApplyPatchRequest
    Output = FsApplyPatchResult

    def run(self, request: FsApplyPatchRequest) -> FsApplyPatchResult:
        # Compute every file's outcome first; only commit once all hunks applied.
        planned = [_plan_file(patch) for patch in _parse_patch(request.patch)]
        changed: list[str] = []
        for target, relative, content in planned:
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            changed.append(relative)
        return FsApplyPatchResult(applied=True, files_changed=changed)


TOOLS = (
    FsReadFile(),
    FsWriteFile(),
    FsListDir(),
    FsGlob(),
    FsGrep(),
    FsMove(),
    FsDelete(),
    FsMkdir(),
    FsStat(),
    FsApplyPatch(),
)

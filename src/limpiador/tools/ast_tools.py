"""``ast.*`` namespace — semantic code understanding (ARCHITECTURE.md §5.3, 12 tools).

parse_file, list_symbols, find_definition, find_references, call_graph,
dependency_tree, find_dead_code, detect_cycles, rename_symbol, extract_function,
list_imports, complexity_score. This is the differentiator: limpiador resolves
*symbols* against a real tree-sitter parse tree, it does not grep for text. The
practical consequence is everywhere — a name in a comment or a string literal is
not a reference, because it is not an ``identifier`` node — and it is what makes
a cross-file rename safe rather than a risky find-and-replace.

It is also the Property #5 anchor: ``find_references`` emits a typed
:class:`RefList`, and that *same object* is handed to ``rename_symbol`` with no
re-parsing in between (the composability chain §8). Python is the first (and,
here, only) language by design (§14); every file resolves ambiently against the
working-tree root, the same way the git and fs tools do.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from limpiador.observability.errors import MalformedInputError, NotFoundError
from limpiador.schemas import (
    AstCallGraph,
    AstCallGraphRequest,
    AstComplexityResult,
    AstComplexityScoreRequest,
    AstCyclesResult,
    AstDeadCodeResult,
    AstDefinition,
    AstDependencyTree,
    AstDependencyTreeRequest,
    AstDetectCyclesRequest,
    AstExtractFunctionRequest,
    AstExtractFunctionResult,
    AstFindDeadCodeRequest,
    AstFindDefinitionRequest,
    AstImportList,
    AstListImportsRequest,
    AstListSymbolsRequest,
    AstParseFileRequest,
    AstParseResult,
    AstRenameResult,
    AstSymbolList,
    CallEdge,
    DependencyEdge,
    FindReferencesRequest,
    ImportCycle,
    ImportInfo,
    Reference,
    RefList,
    RenameSymbolRequest,
    Symbol,
    SymbolComplexity,
)
from limpiador.tools.base import Tool

# The branch-introducing node types a cyclomatic complexity count sums over.
_BRANCH_NODES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "for_statement",
        "while_statement",
        "except_clause",
        "boolean_operator",
        "conditional_expression",
        "assert_statement",
        "case_clause",
    }
)
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"})


# ---- tree-sitter engine + ambient file resolution ---------------------------
_LANGUAGE = Language(tsp.language())


def _parse(source: bytes) -> Node:
    """Parse Python source into a tree-sitter syntax tree's root node."""
    return Parser(_LANGUAGE).parse(source).root_node


def _root() -> Path:
    """The working-tree root every file path resolves against."""
    return Path.cwd().resolve()


def _resolve(file: str) -> Path:
    """Resolve a repo-relative path to an existing file, or raise NotFoundError."""
    candidate = (_root() / file).resolve()
    if not candidate.is_file():
        raise NotFoundError(f"no file at {file!r}")
    return candidate


def _rel(path: Path) -> str:
    """A path's location relative to the root, as a stable posix string."""
    return path.relative_to(_root()).as_posix()


def _python_files(path: str) -> list[Path]:
    """Every ``.py`` file under a repo-relative path (a file resolves to itself)."""
    base = (_root() / path).resolve()
    if base.is_file():
        return [base] if base.suffix == ".py" else []
    if not base.is_dir():
        raise NotFoundError(f"no file or directory at {path!r}")
    return sorted(p for p in base.rglob("*.py") if not (set(p.parts) & _SKIP_DIRS))


def _walk(node: Node) -> Iterator[Node]:
    """Yield every node in the subtree, parents before children, left to right."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _field_text(node: Node, field: str) -> str | None:
    """The decoded text of a node's named field, or None when it is absent."""
    child = node.child_by_field_name(field)
    return child.text.decode() if child is not None else None


# ---- symbol extraction ------------------------------------------------------
def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _method_symbols(class_node: Node, file: str) -> list[Symbol]:
    """The methods defined directly in a class body."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    return [
        Symbol(name=_field_text(child, "name"), kind="method", file=file, line=_line(child))
        for child in body.children
        if child.type == "function_definition"
    ]


def _assignment_name(statement: Node) -> str | None:
    """The left-hand identifier of a module-level ``name = ...`` assignment."""
    for child in statement.children:
        if child.type == "assignment":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                return left.text.decode()
    return None


def _file_symbols(root: Node, file: str) -> list[Symbol]:
    """The top-level functions, classes, methods, and variables defined in a file."""
    symbols: list[Symbol] = []
    for node in root.children:
        if node.type == "function_definition":
            symbols.append(Symbol(name=_field_text(node, "name"), kind="function", file=file, line=_line(node)))
        elif node.type == "class_definition":
            symbols.append(Symbol(name=_field_text(node, "name"), kind="class", file=file, line=_line(node)))
            symbols.extend(_method_symbols(node, file))
        elif node.type == "expression_statement":
            name = _assignment_name(node)
            if name is not None:
                symbols.append(Symbol(name=name, kind="variable", file=file, line=_line(node)))
    return symbols


# ---- identifier resolution (references, dead code) --------------------------
def _is_attribute_name(node: Node) -> bool:
    """True for the ``.attr`` half of an attribute access — not a free reference."""
    parent = node.parent
    return parent is not None and parent.type == "attribute" and parent.child_by_field_name("attribute") == node


def _is_definition_name(node: Node) -> bool:
    """True when the identifier is the name being *defined* by a def/class."""
    parent = node.parent
    if parent is None or parent.type not in ("function_definition", "class_definition"):
        return False
    return parent.child_by_field_name("name") == node


def _identifier_nodes(root: Node, name: str) -> Iterator[Node]:
    """Every identifier node spelling ``name`` that is a real reference site."""
    for node in _walk(root):
        if node.type == "identifier" and node.text.decode() == name and not _is_attribute_name(node):
            yield node


# ---- import extraction ------------------------------------------------------
def _imported_name(node: Node) -> str | None:
    """The bound name of one ``from x import ...`` clause (alias resolved to base)."""
    if node.type == "aliased_import":
        return _field_text(node, "name")
    if node.type in ("dotted_name", "identifier"):
        return node.text.decode()
    return None


def _file_imports(root: Node) -> list[ImportInfo]:
    """Every import in a file as typed :class:`ImportInfo` rows."""
    imports: list[ImportInfo] = []
    for node in _walk(root):
        if node.type == "import_statement":
            for child in node.children:
                module = _imported_name(child)
                if module is not None:
                    imports.append(ImportInfo(module=module, names=[], line=_line(node)))
        elif node.type == "import_from_statement":
            module = _field_text(node, "module_name") or ""
            names = [_imported_name(child) for child in node.children_by_field_name("name")]
            imports.append(ImportInfo(module=module, names=[n for n in names if n], line=_line(node)))
    return imports


# ---- module ↔ file mapping (dependency tree, cycles) ------------------------
def _module_name(path: Path) -> str:
    """The dotted module name for a file, e.g. ``pkg/core.py`` -> ``pkg.core``."""
    parts = list(path.relative_to(_root()).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_file(module: str) -> Path | None:
    """The file backing a dotted module name, if it exists under the root."""
    stem = module.replace(".", "/")
    for candidate in (_root() / f"{stem}.py", _root() / stem / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _local_module(module: str) -> str | None:
    """``module`` if it resolves to a file in this repository, else None."""
    return module if _module_file(module) is not None else None


# ---- call graph -------------------------------------------------------------
def _function_index() -> dict[str, Node]:
    """A name -> definition-node map for every function across the repository."""
    index: dict[str, Node] = {}
    for path in _python_files("."):
        for node in _walk(_parse(path.read_bytes())):
            if node.type == "function_definition":
                index.setdefault(_field_text(node, "name"), node)
    return index


def _calls_in(node: Node) -> list[str]:
    """The names of the functions called directly within a node's subtree."""
    callees: list[str] = []
    for descendant in _walk(node):
        if descendant.type == "call":
            target = descendant.child_by_field_name("function")
            if target is not None and target.type == "identifier":
                callees.append(target.text.decode())
    return callees


# ---- cycle detection (Tarjan strongly-connected components) -----------------
def _strongly_connected(graph: dict[str, list[str]]) -> list[list[str]]:
    """The strongly-connected components of size > 1 — each one an import cycle."""
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    components: list[list[str]] = []
    counter = [0]

    def connect(node: str) -> None:
        order[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for nxt in graph.get(node, []):
            if nxt not in order:
                connect(nxt)
                low[node] = min(low[node], low[nxt])
            elif on_stack.get(nxt):
                low[node] = min(low[node], order[nxt])
        if low[node] == order[node]:
            component = _pop_component(stack, on_stack, node)
            if len(component) > 1:
                components.append(sorted(component))

    for node in list(graph):
        if node not in order:
            connect(node)
    return components


def _pop_component(stack: list[str], on_stack: dict[str, bool], root: str) -> list[str]:
    """Pop one SCC off the Tarjan stack."""
    component: list[str] = []
    while True:
        node = stack.pop()
        on_stack[node] = False
        component.append(node)
        if node == root:
            return component


def _import_graph(files: list[Path]) -> dict[str, list[str]]:
    """A module -> [imported local modules] graph over the given files."""
    graph: dict[str, list[str]] = {}
    for path in files:
        module = _module_name(path)
        deps = []
        for info in _file_imports(_parse(path.read_bytes())):
            local = _local_module(info.module)
            if local is not None and local != module:
                deps.append(local)
        graph[module] = deps
    return graph


# ============================================================================
# The twelve tools
# ============================================================================
class AstParseFile(Tool):
    name = "ast.parse_file"
    description = (
        "Parse a Python source file into a syntax tree and report its size. "
        "Synonyms: parse, syntax tree, AST, tokenize, can this file be read."
    )
    Input = AstParseFileRequest
    Output = AstParseResult

    def run(self, request: AstParseFileRequest) -> AstParseResult:
        root = _parse(_resolve(request.file).read_bytes())
        node_count = sum(1 for _ in _walk(root))
        return AstParseResult(
            file=request.file, language="python", node_count=node_count, ok=not root.has_error
        )


class AstListSymbols(Tool):
    name = "ast.list_symbols"
    description = (
        "List the symbols a file defines — functions, classes, methods, variables. "
        "Synonyms: definitions, outline, members, what is declared here, structure."
    )
    Input = AstListSymbolsRequest
    Output = AstSymbolList

    def run(self, request: AstListSymbolsRequest) -> AstSymbolList:
        root = _parse(_resolve(request.file).read_bytes())
        return AstSymbolList(file=request.file, symbols=_file_symbols(root, request.file))


class AstFindDefinition(Tool):
    name = "ast.find_definition"
    description = (
        "Resolve where a symbol is defined — its file, line, and kind. Synonyms: "
        "go to definition, jump to, where is this declared, locate, source of."
    )
    Input = AstFindDefinitionRequest
    Output = AstDefinition

    def run(self, request: AstFindDefinitionRequest) -> AstDefinition:
        files = [_resolve(request.file)] if request.file else _python_files(".")
        for path in files:
            for symbol in _file_symbols(_parse(path.read_bytes()), _rel(path)):
                if symbol.name == request.symbol:
                    return AstDefinition(symbol=symbol.name, file=symbol.file, line=symbol.line, kind=symbol.kind)
        raise NotFoundError(f"no definition of {request.symbol!r} found")


class AstFindReferences(Tool):
    name = "ast.find_references"
    description = (
        "Find every site a symbol is used across the repository, resolved against "
        "the parse tree — not text in comments or strings. Synonyms: usages, "
        "callers, who uses this, occurrences, references."
    )
    Input = FindReferencesRequest
    Output = RefList

    def run(self, request: FindReferencesRequest) -> RefList:
        references: list[Reference] = []
        for path in _python_files("."):
            rel = _rel(path)
            for node in _identifier_nodes(_parse(path.read_bytes()), request.symbol):
                references.append(
                    Reference(file=rel, line=_line(node), symbol=request.symbol, column=node.start_point[1])
                )
        references.sort(key=lambda ref: (ref.file, ref.line, ref.column or 0))
        return RefList(symbol=request.symbol, references=references)


class AstCallGraphTool(Tool):
    name = "ast.call_graph"
    description = (
        "Build the call graph rooted at a function by traversing real calls. "
        "Synonyms: who calls what, call tree, fan-out, callees, invocation graph."
    )
    Input = AstCallGraphRequest
    Output = AstCallGraph

    def run(self, request: AstCallGraphRequest) -> AstCallGraph:
        index = _function_index()
        edges: list[CallEdge] = []
        seen: set[str] = set()
        frontier = [(request.symbol, 0)]
        while frontier:
            symbol, depth = frontier.pop(0)
            if symbol in seen or depth >= request.depth or symbol not in index:
                continue
            seen.add(symbol)
            for callee in _calls_in(index[symbol]):
                edges.append(CallEdge(caller=symbol, callee=callee))
                frontier.append((callee, depth + 1))
        return AstCallGraph(root=request.symbol, edges=edges)


class AstDependencyTreeTool(Tool):
    name = "ast.dependency_tree"
    description = (
        "Build the local import/dependency tree rooted at a module. Synonyms: "
        "imports graph, what does this depend on, module tree, transitive imports."
    )
    Input = AstDependencyTreeRequest
    Output = AstDependencyTree

    def run(self, request: AstDependencyTreeRequest) -> AstDependencyTree:
        start = _module_name(_resolve(request.file))
        edges: list[DependencyEdge] = []
        seen: set[str] = set()
        frontier = [(start, 0)]
        while frontier:
            module, depth = frontier.pop(0)
            path = _module_file(module)
            if module in seen or depth >= request.depth or path is None:
                continue
            seen.add(module)
            for info in _file_imports(_parse(path.read_bytes())):
                local = _local_module(info.module)
                if local is not None:
                    edges.append(DependencyEdge(module=module, imports=local))
                    frontier.append((local, depth + 1))
        return AstDependencyTree(root=start, edges=edges)


class AstFindDeadCode(Tool):
    name = "ast.find_dead_code"
    description = (
        "Identify functions and classes that are defined but never referenced. "
        "Synonyms: unused code, unreachable, orphan symbols, can this be deleted."
    )
    Input = AstFindDeadCodeRequest
    Output = AstDeadCodeResult

    def run(self, request: AstFindDeadCodeRequest) -> AstDeadCodeResult:
        definitions: list[Symbol] = []
        used: set[str] = set()
        for path in _python_files(request.path):
            root = _parse(path.read_bytes())
            definitions.extend(s for s in _file_symbols(root, _rel(path)) if s.kind in ("function", "class"))
            for node in _walk(root):
                if node.type == "identifier" and not _is_definition_name(node):
                    used.add(node.text.decode())
        return AstDeadCodeResult(symbols=[d for d in definitions if d.name not in used])


class AstDetectCycles(Tool):
    name = "ast.detect_cycles"
    description = (
        "Detect import cycles in the local dependency graph. Synonyms: circular "
        "imports, recursion in imports, cyclic dependencies, mutual imports."
    )
    Input = AstDetectCyclesRequest
    Output = AstCyclesResult

    def run(self, request: AstDetectCyclesRequest) -> AstCyclesResult:
        graph = _import_graph(_python_files(request.path))
        cycles = _strongly_connected(graph)
        return AstCyclesResult(cycles=[ImportCycle(modules=cycle) for cycle in cycles])


class AstRenameSymbol(Tool):
    name = "ast.rename_symbol"
    description = (
        "Rename a symbol at exactly the resolved reference sites and nowhere else. "
        "Consumes a find_references RefList unchanged. Synonyms: rename, refactor "
        "name, safe rename, change identifier everywhere."
    )
    Input = RenameSymbolRequest
    Output = AstRenameResult

    def run(self, request: RenameSymbolRequest) -> AstRenameResult:
        old = request.references.symbol
        by_file: dict[str, list[Reference]] = defaultdict(list)
        for reference in request.references.references:
            by_file[reference.file].append(reference)
        planned: dict[Path, str] = {}
        for file, references in by_file.items():
            lines = _resolve(file).read_text().split("\n")
            for reference in sorted(references, key=lambda r: (r.line, r.column or 0), reverse=True):
                # A reference can be stale (the file changed since it was resolved)
                # or synthesised by the model. Indexing blindly would raise
                # IndexError — not a ToolError, so it would escape the loop's
                # folding and kill the whole run. Fail recoverably instead, so the
                # model can re-resolve and try again.
                if not 1 <= reference.line <= len(lines):
                    raise MalformedInputError(
                        f"{file}:{reference.line} is outside the file "
                        f"(1-{len(lines)}); re-resolve the references with "
                        "ast.find_references before renaming."
                    )
                lines[reference.line - 1] = _replace_at(
                    lines[reference.line - 1], reference.column or 0, old, request.new_name
                )
            planned[_resolve(file)] = "\n".join(lines)
        for path, text in planned.items():
            path.write_text(text)
        return AstRenameResult(
            new_name=request.new_name,
            sites_changed=len(request.references.references),
            files_changed=sorted(by_file),
        )


class AstExtractFunction(Tool):
    name = "ast.extract_function"
    description = (
        "Extract a span of lines into a new function and call it in their place. "
        "Synonyms: extract method, pull out, refactor into function, decompose."
    )
    Input = AstExtractFunctionRequest
    Output = AstExtractFunctionResult

    def run(self, request: AstExtractFunctionRequest) -> AstExtractFunctionResult:
        path = _resolve(request.file)
        lines = path.read_text().split("\n")
        if not 1 <= request.line_start <= request.line_end <= len(lines):
            raise MalformedInputError("the line range is outside the file")
        span = lines[request.line_start - 1 : request.line_end]
        indent = len(span[0]) - len(span[0].lstrip())
        body = "\n".join("    " + line[indent:] for line in span)
        new_function = f"def {request.name}():\n{body}"
        remaining = lines[: request.line_start - 1] + [f"{' ' * indent}{request.name}()"] + lines[request.line_end :]
        definition_line = len(remaining) + 2
        path.write_text("\n".join(remaining + ["", new_function]))
        return AstExtractFunctionResult(file=request.file, function_name=request.name, line=definition_line)


class AstListImports(Tool):
    name = "ast.list_imports"
    description = (
        "List the imports of a module — the modules and the names it brings in. "
        "Synonyms: dependencies, what does this import, from-imports, includes."
    )
    Input = AstListImportsRequest
    Output = AstImportList

    def run(self, request: AstListImportsRequest) -> AstImportList:
        root = _parse(_resolve(request.file).read_bytes())
        return AstImportList(file=request.file, imports=_file_imports(root))


class AstComplexityScore(Tool):
    name = "ast.complexity_score"
    description = (
        "Compute a cyclomatic complexity score per function and for the file. "
        "Synonyms: how complex, branch count, maintainability, cyclomatic, hotspots."
    )
    Input = AstComplexityScoreRequest
    Output = AstComplexityResult

    def run(self, request: AstComplexityScoreRequest) -> AstComplexityResult:
        root = _parse(_resolve(request.file).read_bytes())
        per_symbol = [
            SymbolComplexity(symbol=_field_text(node, "name"), score=_complexity(node))
            for node in _walk(root)
            if node.type == "function_definition"
        ]
        if request.symbol is not None:
            per_symbol = [s for s in per_symbol if s.symbol == request.symbol]
            if not per_symbol:
                raise NotFoundError(f"no function {request.symbol!r} in {request.file!r}")
        return AstComplexityResult(
            file=request.file, score=sum(s.score for s in per_symbol), per_symbol=per_symbol
        )


def _replace_at(line: str, column: int, old: str, new: str) -> str:
    """Splice ``new`` in for ``old`` at a precise column on one line."""
    return line[:column] + new + line[column + len(old) :]


def _complexity(node: Node) -> int:
    """Cyclomatic complexity of a function: one plus its branch-introducing nodes."""
    return 1 + sum(1 for descendant in _walk(node) if descendant.type in _BRANCH_NODES)


TOOLS = (
    AstParseFile(),
    AstListSymbols(),
    AstFindDefinition(),
    AstFindReferences(),
    AstCallGraphTool(),
    AstDependencyTreeTool(),
    AstFindDeadCode(),
    AstDetectCycles(),
    AstRenameSymbol(),
    AstExtractFunction(),
    AstListImports(),
    AstComplexityScore(),
)

"""``ast.*`` namespace — semantic code understanding (ARCHITECTURE.md §5.3, 12 tools).

parse_file, list_symbols, find_definition, find_references, call_graph,
dependency_tree, find_dead_code, detect_cycles, rename_symbol, extract_function,
list_imports, complexity_score. This is the differentiator: limpiador resolves
symbols, traverses call graphs, and performs safe cross-file renames using real
parsing (tree-sitter) rather than shelling out to grep. It is load-bearing for
composability — ``find_references`` → ``rename_symbol`` is the canonical typed
chain (§8) — and is built on the project's primary language by design (§14).
"""

from __future__ import annotations

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
    FindReferencesRequest,
    RefList,
    RenameSymbolRequest,
)
from limpiador.tools.base import declared_tool

TOOLS = (
    declared_tool("ast.parse_file", "Parse a source file into a syntax tree.", AstParseFileRequest, AstParseResult),
    declared_tool("ast.list_symbols", "List the symbols (functions, classes, variables) defined in a file.", AstListSymbolsRequest, AstSymbolList),
    declared_tool("ast.find_definition", "Resolve where a symbol is defined.", AstFindDefinitionRequest, AstDefinition),
    declared_tool("ast.find_references", "Find every site a symbol is used across the repository.", FindReferencesRequest, RefList),
    declared_tool("ast.call_graph", "Build the call graph rooted at a function.", AstCallGraphRequest, AstCallGraph),
    declared_tool("ast.dependency_tree", "Build the import/dependency tree for a module.", AstDependencyTreeRequest, AstDependencyTree),
    declared_tool("ast.find_dead_code", "Identify unreferenced functions and symbols.", AstFindDeadCodeRequest, AstDeadCodeResult),
    declared_tool("ast.detect_cycles", "Detect import cycles in the dependency graph.", AstDetectCyclesRequest, AstCyclesResult),
    declared_tool("ast.rename_symbol", "Rename a symbol across all of its references safely.", RenameSymbolRequest, AstRenameResult),
    declared_tool("ast.extract_function", "Extract a span of code into a new function.", AstExtractFunctionRequest, AstExtractFunctionResult),
    declared_tool("ast.list_imports", "List the imports of a module.", AstListImportsRequest, AstImportList),
    declared_tool("ast.complexity_score", "Compute a complexity score for a function or file.", AstComplexityScoreRequest, AstComplexityResult),
)

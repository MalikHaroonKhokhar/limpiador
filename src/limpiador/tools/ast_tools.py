"""``ast.*`` namespace — semantic code understanding (ARCHITECTURE.md §5.3, 12 tools).

parse_file, list_symbols, find_definition, find_references, call_graph,
dependency_tree, find_dead_code, detect_cycles, rename_symbol, extract_function,
list_imports, complexity_score. This is the differentiator: limpiador resolves
symbols, traverses call graphs, and performs safe cross-file renames using real
parsing (tree-sitter) rather than shelling out to grep. It is load-bearing for
composability — ``find_references`` → ``rename_symbol`` is the canonical typed
chain (§8) — and is built on the project's primary language by design (§14).
"""

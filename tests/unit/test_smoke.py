"""Smoke test — the package and every layer module import (ARCHITECTURE.md §3-4).

The foundational check the bootstrap ticket exists to satisfy: the layered
package is wired up correctly and nothing in the skeleton fails at import time.
Every later ticket has a home only if these homes can be imported, so this test
runs first in the unit tier and guards the layering from day one.

It depends on nothing but the package itself — no model, no network, no temp
repo — which is exactly what a smoke test should be.
"""

from __future__ import annotations

import importlib

import pytest

# Every module in the layered package, named explicitly so a missing or
# unimportable module is a hard, legible failure rather than a silent gap.
LAYER_MODULES = [
    # Layer 4 — interface
    "limpiador.cli",
    # Layer 3 — agent core / orchestration
    "limpiador.agent",
    "limpiador.agent.loop",
    "limpiador.agent.context",
    "limpiador.agent.llm",
    "limpiador.agent.guard",
    # Layer 2 — tool subsystem (the five namespaces + registry + base)
    "limpiador.tools",
    "limpiador.tools.base",
    "limpiador.tools.registry",
    "limpiador.tools.git_tools",
    "limpiador.tools.github_tools",
    "limpiador.tools.fs_tools",
    "limpiador.tools.ast_tools",
    "limpiador.tools.test_tools",
    # Subagents
    "limpiador.subagents",
    "limpiador.subagents.reviewer",
    # Layer 1 — foundations / plumbing
    "limpiador.observability",
    "limpiador.observability.tracing",
    "limpiador.observability.errors",
    "limpiador.observability.retry",
    "limpiador.schemas",
]


def test_package_imports_and_is_versioned() -> None:
    """The top-level package imports and exposes a version string."""
    import limpiador

    assert isinstance(limpiador.__version__, str)
    assert limpiador.__version__


@pytest.mark.parametrize("module_name", LAYER_MODULES)
def test_layer_module_is_importable(module_name: str) -> None:
    """Each layer module imports cleanly — every later ticket has a home."""
    assert importlib.import_module(module_name) is not None


def test_typed_error_hierarchy_is_rooted_in_tool_error() -> None:
    """The error subclasses all descend from ToolError (CLEAN_CODE.md §6)."""
    from limpiador.observability.errors import (
        MalformedInputError,
        NotFoundError,
        PermissionDeniedError,
        ToolError,
        TransientError,
    )

    for subclass in (
        NotFoundError,
        PermissionDeniedError,
        TransientError,
        MalformedInputError,
    ):
        assert issubclass(subclass, ToolError)

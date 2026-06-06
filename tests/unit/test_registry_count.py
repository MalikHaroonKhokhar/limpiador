"""The registry-size proof — Property #1 cleared (HAR-21).

Property #1 is the dynamic tool registry at scale: the model sees only the fixed
core meta-tools, and discovers the rest. This test pins the *whole* surface: all
five tool namespaces are now implemented, and the registry holds exactly 56
declared tools (12 git + 14 github + 10 fs + 12 ast + 8 verification). That is
the concrete "50+ tools" the property promises — and the count is asserted at the
application registry that the agent loop actually runs against, not a fixture.
"""

from __future__ import annotations

from collections import Counter

from limpiador.tools.registry import REGISTRY

# The per-namespace census the 56 is composed of. Note that the eight
# verification tools span two namespace surfaces — ``test`` (six local tools) and
# ``ci`` (two that compose the github API) — exactly as ARCHITECTURE.md §5.3
# groups them.
_EXPECTED_PER_NAMESPACE = {
    "git": 12,
    "github": 14,
    "fs": 10,
    "ast": 12,
    "test": 6,
    "ci": 2,
}
_TOTAL = 56


def _namespace(name: str) -> str:
    return name.split(".", 1)[0]


def test_the_registry_holds_exactly_fifty_six_tools() -> None:
    assert len(REGISTRY.tool_names()) == _TOTAL


def test_the_count_clears_property_ones_fifty_plus() -> None:
    assert len(REGISTRY.tool_names()) >= 50


def test_every_namespace_is_present_at_its_expected_size() -> None:
    census = Counter(_namespace(name) for name in REGISTRY.tool_names())
    assert dict(census) == _EXPECTED_PER_NAMESPACE


def test_the_five_verification_tools_plus_core_are_the_only_surfaces() -> None:
    # The 56 are spread across exactly the namespaces the architecture names —
    # no stray namespace has leaked in.
    namespaces = {_namespace(name) for name in REGISTRY.tool_names()}
    assert namespaces == set(_EXPECTED_PER_NAMESPACE)

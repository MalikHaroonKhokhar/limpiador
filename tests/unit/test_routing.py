"""Unit tests for model routing and prompt-cache prefix stability (§10).

Two cost levers live in the adapter, and both are tested here without touching
the network:

* **Routing.** Tool-dispatch turns — the bulk of a long run — go to the cheap
  model; the reasoning-heavy opening (planning, and likewise final synthesis)
  escalates to the strong model. The model names and their prices are named
  config, so swapping them is a config edit, not a code change.
* **Prompt caching.** The stable request head (the system instruction plus the
  always-present core tool schemas) is held byte-identical turn after turn, so
  the provider's prompt cache keeps hitting as the conversation tail grows and
  tools are loaded behind it. We assert the prefix fingerprint is stable.

The routing decision is also recorded in the trace, which a recording tracer
verifies here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from limpiador.agent.llm import (
    DEFAULT_ROUTING,
    ModelTier,
    OpenAIAdapter,
    RoutingConfig,
    TurnKind,
    cache_prefix,
    classify_turn,
    prefix_fingerprint,
)
from limpiador.observability.tracing import ROUTING_TAG
from limpiador.tools.registry import CORE_TOOL_NAMES

_SYSTEM = {"role": "system", "content": "You are limpiador."}
_USER = {"role": "user", "content": "tidy the billing module"}
_ASSISTANT = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
_TOOL_RESULT = {"role": "tool", "tool_call_id": "c1", "name": "fs_read_file", "content": "..."}


def _schema(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _core_schemas() -> list[dict[str, Any]]:
    return [_schema(name) for name in CORE_TOOL_NAMES]


# ---- stub client (no network, no key) ---------------------------------------
class _StubClient:
    def __init__(self) -> None:
        self.create_kwargs: dict[str, Any] = {}
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=None,
        )

        def create(**kwargs: Any) -> SimpleNamespace:
            self.create_kwargs = kwargs
            return response

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _adapter(**kwargs: Any) -> tuple[OpenAIAdapter, _StubClient]:
    client = _StubClient()
    return OpenAIAdapter(client=client, **kwargs), client


# ---- turn classification ----------------------------------------------------
def test_the_opening_turn_with_no_tool_results_is_planning() -> None:
    assert classify_turn([_SYSTEM, _USER]) is TurnKind.PLANNING


def test_a_turn_holding_tool_results_is_dispatch() -> None:
    assert classify_turn([_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT]) is TurnKind.DISPATCH


# ---- the routing map --------------------------------------------------------
def test_planning_escalates_to_the_strong_model() -> None:
    tier = DEFAULT_ROUTING.tier_for(TurnKind.PLANNING)
    assert tier.name == DEFAULT_ROUTING.strong.name
    assert tier is DEFAULT_ROUTING.strong


def test_dispatch_takes_the_cheap_model() -> None:
    tier = DEFAULT_ROUTING.tier_for(TurnKind.DISPATCH)
    assert tier.name == DEFAULT_ROUTING.cheap.name
    assert tier is DEFAULT_ROUTING.cheap


def test_the_default_tiers_are_distinct_models_with_prices() -> None:
    assert DEFAULT_ROUTING.cheap.name != DEFAULT_ROUTING.strong.name
    # the cheap tier is genuinely cheaper on both input and output
    assert DEFAULT_ROUTING.cheap.input_usd_per_million < DEFAULT_ROUTING.strong.input_usd_per_million
    assert DEFAULT_ROUTING.cheap.output_usd_per_million < DEFAULT_ROUTING.strong.output_usd_per_million


# ---- routing through the adapter --------------------------------------------
def test_a_dispatch_turn_is_sent_to_the_cheap_model() -> None:
    adapter, client = _adapter()
    adapter.complete([_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT])
    assert client.create_kwargs["model"] == DEFAULT_ROUTING.cheap.name


def test_a_planning_turn_is_sent_to_the_strong_model() -> None:
    adapter, client = _adapter()
    adapter.complete([_SYSTEM, _USER])
    assert client.create_kwargs["model"] == DEFAULT_ROUTING.strong.name


def test_an_explicit_model_pins_every_turn_and_bypasses_routing() -> None:
    adapter, client = _adapter(model="pinned-model")

    adapter.complete([_SYSTEM, _USER])  # would be planning → strong
    assert client.create_kwargs["model"] == "pinned-model"
    adapter.complete([_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT])  # would be dispatch → cheap
    assert client.create_kwargs["model"] == "pinned-model"


# ---- config-driven: swap models with no code change -------------------------
def test_a_custom_routing_config_swaps_model_names() -> None:
    config = RoutingConfig(
        strong=ModelTier("acme-giant", 9.0, 30.0),
        cheap=ModelTier("acme-nano", 0.05, 0.20),
    )
    assert config.tier_for(TurnKind.PLANNING).name == "acme-giant"
    assert config.tier_for(TurnKind.DISPATCH).name == "acme-nano"

    adapter, client = _adapter(routing=config)
    adapter.complete([_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT])
    assert client.create_kwargs["model"] == "acme-nano"


# ---- prompt-cache prefix stability ------------------------------------------
def test_the_stable_prefix_is_byte_identical_as_the_conversation_grows() -> None:
    tools = _core_schemas()
    opening = prefix_fingerprint([_SYSTEM, _USER], tools)
    later = prefix_fingerprint(
        [_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT, _ASSISTANT, _TOOL_RESULT], tools
    )
    assert opening == later  # the growing tail does not perturb the cached head


def test_dynamically_loaded_tools_do_not_change_the_stable_prefix() -> None:
    core_only = prefix_fingerprint([_SYSTEM], _core_schemas())
    with_loaded = prefix_fingerprint(
        [_SYSTEM], [*_core_schemas(), _schema("fs_read_file"), _schema("ast_rename_symbol")]
    )
    assert core_only == with_loaded  # only system + core schemas form the prefix


def test_the_prefix_captures_only_system_and_core_not_the_tail() -> None:
    prefix = cache_prefix([_SYSTEM, _USER, _TOOL_RESULT], [*_core_schemas(), _schema("fs_read_file")])
    assert prefix["system"] == [_SYSTEM]
    assert [c["function"]["name"] for c in prefix["core_tools"]] == list(CORE_TOOL_NAMES)


def test_changing_the_system_prompt_changes_the_prefix() -> None:
    a = prefix_fingerprint([_SYSTEM], _core_schemas())
    b = prefix_fingerprint([{"role": "system", "content": "different"}], _core_schemas())
    assert a != b


# ---- the routing decision is observable -------------------------------------
def test_the_routing_decision_is_recorded_in_the_trace() -> None:
    events: list[tuple[str, str]] = []

    def tracer(tag: str, message: str = "") -> None:
        events.append((tag, message))

    adapter, _ = _adapter(tracer=tracer)
    adapter.complete([_SYSTEM, _USER, _ASSISTANT, _TOOL_RESULT])

    routing_events = [m for tag, m in events if tag == ROUTING_TAG]
    assert routing_events, "the adapter should record its routing decision"
    assert DEFAULT_ROUTING.cheap.name in routing_events[0]
    assert TurnKind.DISPATCH.value in routing_events[0]

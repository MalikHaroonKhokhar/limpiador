"""The LLM adapter — the ONLY provider-specific file (ARCHITECTURE.md §10).

All OpenAI-specific logic is quarantined behind a single interface: take
messages and the active tool schemas, return a response with text and tool
calls. Two implementations satisfy that interface — the real OpenAI adapter and
the deterministic mock used in tests — so the provider is swappable and the mock
is injectable without the agent core ever knowing which it holds. OpenAI types
must not leak past this module into the loop, the tools, or the schemas
(.clauderules §5).

Model routing (cheap-by-default, escalate-for-planning) and prompt-prefix
stability for caching also live here; the model names and prices are treated as
configuration to be verified against current pricing, never hard-coded.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import openai

from limpiador.observability.errors import ConfigError, TransientError
from limpiador.observability.retry import Resilience, resilient_call
from limpiador.observability.tracing import ROUTING_TAG, emit
from limpiador.schemas import LLMResponse, TokenUsage, ToolCall
from limpiador.tools.registry import CORE_TOOL_NAMES

# The provider failures worth retrying: timeouts, dropped connections, rate
# limits, and 5xx server errors. A bad request or an auth error is *not* here —
# retrying those just wastes calls — so they propagate unretried (CLEAN_CODE §6).
_TRANSIENT_OPENAI_ERRORS = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)

# Configuration, not literals (CLEAN_CODE.md §7): the key env var and the model
# override env var.
_API_KEY_ENV = "OPENAI_API_KEY"
# Public: the env var that overrides the model. The CLI's --model flag sets it,
# so the override flows through the registration seam without the CLI importing
# any concrete adapter (it sets documented config, not provider internals). When
# set it *pins* one model for every turn, bypassing routing.
OPENAI_MODEL_ENV = "LIMPIADOR_OPENAI_MODEL"

# The tracer the adapter records routing decisions through: ``(tag, message)``,
# matching :func:`limpiador.observability.tracing.emit`.
Tracer = Callable[[str, str], None]


# ---- Model routing (cheap-by-default, escalate-for-reasoning) ----------------
# ARCHITECTURE.md §10: the bulk of a long run is mechanical tool dispatch, which
# the cheap model handles; the reasoning-heavy turns (planning and synthesis)
# escalate to the strong model. Model names *and prices* are named config — to be
# verified against current OpenAI pricing, never treated as hard facts — so the
# whole policy is retuned by editing config or injecting a RoutingConfig.


class TurnKind(str, Enum):
    """What a model turn is for — the signal routing keys on."""

    PLANNING = "planning"  # reasoning-heavy: forming the approach, or synthesizing
    DISPATCH = "dispatch"  # mechanical: selecting and calling tools


@dataclass(frozen=True)
class ModelTier:
    """A model and its current price (USD per million tokens), as configuration.

    The prices are observability metadata — they let a run report what it spent —
    and must be verified against current OpenAI pricing, not trusted as facts.
    """

    name: str
    input_usd_per_million: float
    output_usd_per_million: float


@dataclass(frozen=True)
class RoutingConfig:
    """Which model serves each turn kind. Swap the tiers to retune, no code change."""

    strong: ModelTier
    cheap: ModelTier

    def tier_for(self, kind: TurnKind) -> ModelTier:
        """Dispatch turns take the cheap model; everything else escalates."""
        return self.cheap if kind is TurnKind.DISPATCH else self.strong


# Default routing. Prices are a starting point to verify against current OpenAI
# pricing (ARCHITECTURE.md §10), never a fact baked into code.
DEFAULT_ROUTING = RoutingConfig(
    strong=ModelTier("gpt-4o", input_usd_per_million=2.50, output_usd_per_million=10.00),
    cheap=ModelTier("gpt-4o-mini", input_usd_per_million=0.15, output_usd_per_million=0.60),
)


def classify_turn(messages: Messages) -> TurnKind:
    """Read the turn's purpose from the transcript shape (ARCHITECTURE.md §10).

    Before any tool result exists, the model is forming its approach — planning,
    the reasoning-heavy work that earns the strong model (final synthesis is the
    same shape of reasoning). Once tool results are in hand, each turn is
    mechanical tool selection — dispatch — and the cheap model serves it. Because
    a long run is mostly dispatch, the bulk of calls stay cheap.
    """
    has_tool_result = any(m.get("role") == "tool" for m in messages)
    return TurnKind.DISPATCH if has_tool_result else TurnKind.PLANNING


def cache_prefix(
    messages: Messages,
    tools: ToolSchemas | None,
    *,
    core_tool_names: tuple[str, ...] = CORE_TOOL_NAMES,
) -> dict[str, Any]:
    """The byte-stable request head the provider's prompt cache reuses.

    It is exactly the part that does not change as a run proceeds: the system
    instruction and the always-present core tool schemas. The volatile
    conversation tail (user/assistant/tool messages) and any dynamically loaded
    tool schemas are excluded, so this head stays identical turn after turn and
    the cache keeps hitting.
    """
    system = [m for m in messages if m.get("role") == "system"]
    core = [t for t in (tools or []) if _schema_name(t) in core_tool_names]
    return {"system": system, "core_tools": core}


def prefix_fingerprint(
    messages: Messages,
    tools: ToolSchemas | None,
    *,
    core_tool_names: tuple[str, ...] = CORE_TOOL_NAMES,
) -> str:
    """A stable fingerprint of the cacheable prefix.

    Serialized with sorted keys and no whitespace, so "the same prefix" means
    byte-identical bytes — never a dict-ordering accident that would silently
    miss the cache. Recorded in the trace so prefix stability is observable.
    """
    canonical = json.dumps(
        cache_prefix(messages, tools, core_tool_names=core_tool_names),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_name(schema: dict[str, Any]) -> str:
    """The function name out of an OpenAI flat tool schema, or '' if malformed."""
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return ""


def _priced(tier: ModelTier | None, usage: TokenUsage | None) -> float | None:
    """The USD cost of one call from its tier's per-million prices, or None.

    None when the tier is unknown (a pinned model has no configured price) or the
    provider returned no usage — both honest "we cannot price this" signals.
    """
    if tier is None or usage is None:
        return None
    return (
        usage.prompt_tokens * tier.input_usd_per_million
        + usage.completion_tokens * tier.output_usd_per_million
    ) / 1_000_000

# The agent core speaks these shapes; provider types never cross this boundary.
Messages = list[dict[str, Any]]
ToolSchemas = list[dict[str, Any]]

# Run-mode selection (ARCHITECTURE.md §12, .clauderules §7). Production knows
# only its own mode name and which mode is the default; every other mode is
# discovered through the registry below, never branched on here.
_LLM_MODE_ENV = "LIMPIADOR_LLM"
_MODE_OPENAI = "openai"
_DEFAULT_MODE = _MODE_OPENAI

# The adapter registry — the seam that keeps the provider swappable without the
# core importing or branching on any concrete adapter. Modes are looked up here
# generically; an adapter the core does not own (the test mock) becomes
# selectable purely by registering itself, so production never names it
# (.clauderules §5).
AdapterFactory = Callable[[], "LLMAdapter"]
_ADAPTER_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(mode: str, factory: AdapterFactory) -> None:
    """Register an adapter factory under a run-mode name.

    This is the extension point through which an adapter the core does not import
    — most importantly the deterministic test mock — becomes selectable by
    ``LIMPIADOR_LLM`` without any production code referencing it.
    """
    _ADAPTER_REGISTRY[mode] = factory


def build_adapter(adapter: LLMAdapter | None = None) -> LLMAdapter:
    """Resolve the model adapter for the current run mode (ARCHITECTURE.md §10, §12).

    An explicitly injected adapter always wins. Otherwise the mode named by
    ``LIMPIADOR_LLM`` (defaulting to OpenAI) is looked up in the registry — a
    generic dict lookup, with no special case for any particular mode. A mode
    that is not registered (e.g. ``mock`` in a production process where the
    test-support layer was never loaded) is a typed configuration error.
    """
    if adapter is not None:
        return adapter
    mode = os.environ.get(_LLM_MODE_ENV, _DEFAULT_MODE)
    factory = _ADAPTER_REGISTRY.get(mode)
    if factory is None:
        raise ConfigError(
            f"{_LLM_MODE_ENV}={mode!r} is not available; "
            f"registered modes: {sorted(_ADAPTER_REGISTRY)}."
        )
    return factory()


class LLMAdapter(ABC):
    """The single interface every model provider satisfies (ARCHITECTURE.md §10).

    The agent core depends only on this; the real adapter and the test mock both
    implement it, so the provider is swappable and the mock is injectable without
    the loop ever knowing which it holds.
    """

    @abstractmethod
    def complete(self, messages: Messages, tools: ToolSchemas | None = None) -> LLMResponse:
        """Send messages + the active tool schemas; return a normalized response."""


class OpenAIAdapter(LLMAdapter):
    """The real OpenAI adapter — the only code that touches the openai SDK.

    A client may be injected (the tests pass a stub), in which case no key is
    needed; otherwise a real client is built from ``OPENAI_API_KEY`` and an
    absent key is a typed :class:`ConfigError`, not a crash.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        routing: RoutingConfig | None = None,
        tracer: Tracer = emit,
        resilience: Resilience | None = None,
        temperature: float | None = None,
    ) -> None:
        # An explicit model (flag or env) pins one model for every turn, bypassing
        # routing — the operator's escape hatch. Otherwise routing decides per turn.
        self._pinned_model = model or os.environ.get(OPENAI_MODEL_ENV) or None
        # When set, sampling temperature is sent on every request; left None it is
        # omitted, so the provider default stands — production behaviour unchanged.
        # The eval harness pins it to 0 so its pass/fail gate is deterministic.
        self._temperature = temperature
        self._routing = routing or DEFAULT_ROUTING
        self._trace = tracer
        # Resilience for the one external call this adapter makes (§13): a token
        # bucket throttles it and bounded backoff retries transient provider
        # failures. One bucket per adapter, applied at this boundary — not scattered.
        self._resilience = resilience or Resilience()
        self._retry = self._resilience.retry
        self._sleep = self._resilience.sleep
        self._limiter = self._resilience.bucket()
        self._client = client if client is not None else self._build_client(api_key)

    @staticmethod
    def _build_client(api_key: str | None) -> Any:
        """Construct a real OpenAI client, or raise ConfigError if no key is set."""
        key = api_key or os.environ.get(_API_KEY_ENV)
        if not key:
            raise ConfigError(
                f"{_API_KEY_ENV} is not set; real mode requires an OpenAI API key. "
                "Use mock mode for offline development."
            )
        return openai.OpenAI(api_key=key)

    def complete(self, messages: Messages, tools: ToolSchemas | None = None) -> LLMResponse:
        kind = classify_turn(messages)
        # A pinned model has no known tier price; a routed one does, which is what
        # lets us annotate the response with a per-call cost for the trace.
        tier = None if self._pinned_model else self._routing.tier_for(kind)
        model = self._pinned_model or (tier.name if tier else "")
        self._record_routing(kind, model, messages, tools)
        request: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            request["tools"] = tools
        if self._temperature is not None:
            request["temperature"] = self._temperature
        response = self._create(request)
        return self._parse_response(response, model=model, route=kind.value, tier=tier)

    def _create(self, request: dict[str, Any]) -> Any:
        """The one external call, made resilient: rate-limited and retried (§13).

        A transient provider failure (timeout, connection, rate limit, 5xx) is
        translated into a typed ``TransientError`` so the shared retry backs off
        and retries it; a non-transient provider error (a bad request, an auth
        failure) is not caught here, so it propagates unretried.
        """

        def call() -> Any:
            try:
                return self._client.chat.completions.create(**request)
            except _TRANSIENT_OPENAI_ERRORS as error:
                raise TransientError(f"openai transient failure: {error}") from error

        return resilient_call(call, limiter=self._limiter, policy=self._retry, sleep=self._sleep)

    def _record_routing(
        self, kind: TurnKind, model: str, messages: Messages, tools: ToolSchemas | None
    ) -> None:
        """Record the routing decision and the stable-prefix fingerprint in the trace."""
        fingerprint = prefix_fingerprint(messages, tools)
        self._trace(ROUTING_TAG, f"{kind.value} -> {model} (stable-prefix {fingerprint[:12]})")

    def _parse_response(
        self,
        response: Any,
        *,
        model: str = "",
        route: str | None = None,
        tier: ModelTier | None = None,
    ) -> LLMResponse:
        """Normalize a provider response into a typed LLMResponse.

        The routing annotations (``model``, ``route``, and a priced ``cost_usd``
        when the model's tier is known) ride along so the loop can record a fully
        structured model-call trace entry without reaching back into the adapter.
        """
        message = response.choices[0].message
        usage = self._parse_usage(response.usage)
        return LLMResponse(
            text=message.content,
            tool_calls=self._parse_tool_calls(message),
            usage=usage,
            model=model or None,
            route=route,
            cost_usd=_priced(tier, usage),
        )

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[ToolCall]:
        """Map provider tool calls (parallel calls included) to typed ToolCalls."""
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw_arguments = call.function.arguments
            arguments = json.loads(raw_arguments) if raw_arguments else {}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
        return calls

    @staticmethod
    def _parse_usage(usage: Any) -> TokenUsage | None:
        """Map provider token usage to TokenUsage, or None when absent."""
        if usage is None:
            return None
        return TokenUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )


# The only adapter production owns. Other modes (the test mock) register
# themselves from their own layer; the core does not import them.
register_adapter(_MODE_OPENAI, OpenAIAdapter)

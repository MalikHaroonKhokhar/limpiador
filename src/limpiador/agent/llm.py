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

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import openai

from limpiador.observability.errors import ConfigError
from limpiador.schemas import LLMResponse, TokenUsage, ToolCall

# Configuration, not literals (CLEAN_CODE.md §7): the key env var, the model
# override env var, and the cheap default model. The default is a starting
# point to verify against current pricing (ARCHITECTURE.md §10), not a fact.
_API_KEY_ENV = "OPENAI_API_KEY"
_MODEL_ENV = "LIMPIADOR_OPENAI_MODEL"
_DEFAULT_MODEL = "gpt-4o-mini"

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
    ) -> None:
        self._model = model or os.environ.get(_MODEL_ENV) or _DEFAULT_MODEL
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
        request: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            request["tools"] = tools
        response = self._client.chat.completions.create(**request)
        return self._parse_response(response)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Normalize a provider response into a typed LLMResponse."""
        message = response.choices[0].message
        return LLMResponse(
            text=message.content,
            tool_calls=self._parse_tool_calls(message),
            usage=self._parse_usage(response.usage),
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

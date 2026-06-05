"""Real-mode smoke for the OpenAI adapter (ARCHITECTURE.md §11, §12).

A single, deliberately tiny live call confirming the adapter talks to the real
API and the response *shape* round-trips into a typed LLMResponse with usage.
Marked ``e2e`` so it is excluded from the default `make test`, and skipped
gracefully when ``OPENAI_API_KEY`` is unset so CI stays green and no credit is
spent. Run it via `make test-e2e`.
"""

from __future__ import annotations

import os

import pytest

from limpiador.agent.llm import OpenAIAdapter
from limpiador.schemas import LLMResponse

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set — real-mode only"
)
def test_live_cheap_call_returns_llm_response_shape() -> None:
    adapter = OpenAIAdapter()

    response = adapter.complete(
        messages=[{"role": "user", "content": "Reply with the single word: pong"}]
    )

    assert isinstance(response, LLMResponse)
    assert response.usage is not None
    assert response.usage.total_tokens > 0

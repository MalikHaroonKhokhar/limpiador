"""Test-support layer (ARCHITECTURE.md §3, §10-11, .clauderules §5).

Infrastructure that exists only for the tests — most importantly the
deterministic mock LLM. Nothing here is ever imported by ``src/limpiador/``; it
crosses into the agent core solely by being injected through the ``LLMAdapter``
interface, so production code never knows a mock exists.

Importing this package registers the mock under ``LIMPIADOR_LLM=mock`` (see
:mod:`support.mock_llm`), so selecting mock mode works whenever the test-support
layer is loaded — and only then.
"""

from . import mock_llm as mock_llm

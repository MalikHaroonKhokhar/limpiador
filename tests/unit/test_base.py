"""Unit tests for the Tool ABC (ARCHITECTURE.md §5, tools/base.py).

Cover the three guarantees the base class exists to enforce: the namespace
convention (a definition-time error for anything off the allowed surface), typed
I/O wired to pydantic, and an ``openai_schema()`` that emits a function schema
the OpenAI API will actually accept (notably: a function name with no dots).
"""

from __future__ import annotations

import re

import pytest

from limpiador.observability.errors import MalformedInputError
from limpiador.schemas import FindReferencesRequest, RefList
from limpiador.tools.base import NAMESPACES, Tool

# OpenAI function names must match this; dots are NOT permitted, so a namespaced
# tool name has to be transformed before it crosses the wire.
_OPENAI_FUNCTION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class _FindReferencesTool(Tool):
    """A real, minimal tool used as the sample under test."""

    name = "ast.find_references"
    description = "Locate every usage / call site of a symbol across the repository."
    Input = FindReferencesRequest
    Output = RefList

    def run(self, request: FindReferencesRequest) -> RefList:
        return RefList(symbol=request.symbol, references=[])


class _WrongOutputTool(Tool):
    """A buggy tool whose run() violates its own Output contract on purpose."""

    name = "git.status"
    description = "Returns the wrong type so the output contract can be tested."
    Input = FindReferencesRequest
    Output = RefList

    def run(self, request: FindReferencesRequest) -> RefList:
        return "not a RefList"  # type: ignore[return-value]


def test_invoke_validates_input_runs_and_returns_typed_output() -> None:
    result = _FindReferencesTool().invoke(
        FindReferencesRequest(file="billing.py", symbol="calculate_total")
    )
    assert isinstance(result, RefList)


def test_invoke_coerces_a_raw_dict_into_the_typed_input() -> None:
    """The loop hands raw tool-call arguments; invoke builds the Input from them."""
    result = _FindReferencesTool().invoke({"file": "billing.py", "symbol": "x"})
    assert isinstance(result, RefList)
    assert result.symbol == "x"


def test_invoke_rejects_a_dict_that_violates_the_input_contract() -> None:
    with pytest.raises(MalformedInputError):
        _FindReferencesTool().invoke({"file": "billing.py"})  # missing 'symbol'


def test_invoke_rejects_the_wrong_input_type() -> None:
    with pytest.raises(MalformedInputError):
        _FindReferencesTool().invoke(RefList(symbol="x"))  # not a FindReferencesRequest


def test_invoke_rejects_output_that_breaks_the_contract() -> None:
    with pytest.raises(TypeError):
        _WrongOutputTool().invoke(FindReferencesRequest(file="a.py", symbol="x"))


def test_namespace_is_derived_from_the_name() -> None:
    assert _FindReferencesTool.namespace() == "ast"
    assert _FindReferencesTool.namespace() in NAMESPACES


def test_openai_schema_shape_is_a_function_tool() -> None:
    schema = _FindReferencesTool.openai_schema()

    assert schema["type"] == "function"
    assert set(schema["function"]) >= {"name", "description", "parameters"}


def test_openai_schema_name_has_no_dots_and_is_valid() -> None:
    name = _FindReferencesTool.openai_schema()["function"]["name"]

    assert "." not in name
    assert name == "ast_find_references"
    assert _OPENAI_FUNCTION_NAME.match(name)


def test_openai_schema_parameters_are_a_strict_object_schema() -> None:
    parameters = _FindReferencesTool.openai_schema()["function"]["parameters"]

    assert parameters["type"] == "object"
    assert "properties" in parameters
    assert "symbol" in parameters["properties"]
    assert parameters["additionalProperties"] is False


def test_openai_schema_description_is_carried_through() -> None:
    schema = _FindReferencesTool.openai_schema()
    assert schema["function"]["description"] == _FindReferencesTool.description


def test_concrete_tool_runs_and_returns_typed_output() -> None:
    result = _FindReferencesTool().run(
        FindReferencesRequest(file="billing.py", symbol="calculate_total")
    )
    assert isinstance(result, RefList)
    assert result.symbol == "calculate_total"


def test_tool_without_run_cannot_be_instantiated() -> None:
    class _Incomplete(Tool):
        name = "git.status"
        description = "Show the working-tree status."
        Input = FindReferencesRequest
        Output = RefList

    with pytest.raises(TypeError):
        _Incomplete()


def test_unknown_namespace_is_rejected_at_definition() -> None:
    with pytest.raises(ValueError, match="namespace"):

        class _Bad(Tool):
            name = "frontend.render"
            description = "x"
            Input = FindReferencesRequest
            Output = RefList

            def run(self, request: FindReferencesRequest) -> RefList:  # pragma: no cover
                return RefList(symbol="x")


def test_unnamespaced_name_is_rejected_at_definition() -> None:
    with pytest.raises(ValueError):

        class _Bad(Tool):
            name = "status"
            description = "x"
            Input = FindReferencesRequest
            Output = RefList

            def run(self, request: FindReferencesRequest) -> RefList:  # pragma: no cover
                return RefList(symbol="x")


def test_multiple_dots_in_name_is_rejected() -> None:
    with pytest.raises(ValueError):

        class _Bad(Tool):
            name = "git.sub.status"
            description = "x"
            Input = FindReferencesRequest
            Output = RefList

            def run(self, request: FindReferencesRequest) -> RefList:  # pragma: no cover
                return RefList(symbol="x")


def test_empty_description_is_rejected() -> None:
    with pytest.raises(ValueError, match="description"):

        class _Bad(Tool):
            name = "git.status"
            description = "   "
            Input = FindReferencesRequest
            Output = RefList

            def run(self, request: FindReferencesRequest) -> RefList:  # pragma: no cover
                return RefList(symbol="x")


def test_untyped_io_is_rejected() -> None:
    with pytest.raises(ValueError, match="Schema"):

        class _Bad(Tool):
            name = "git.status"
            description = "Show status."
            Input = dict  # not a limpiador Schema
            Output = RefList

            def run(self, request: object) -> RefList:  # pragma: no cover
                return RefList(symbol="x")

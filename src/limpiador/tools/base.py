"""The Tool contract and namespace conventions (ARCHITECTURE.md §5).

Defines the :class:`Tool` ABC every tool implements: a namespaced name, a
description used by ``search_tools`` ranking, a typed input/output contract from
:mod:`limpiador.schemas`, and a single ``run`` action (CLEAN_CODE.md §2 — one
function does one thing). A tool emits a typed object and raises a typed
``ToolError`` on failure; it never returns free text and never returns a
sentinel (CLEAN_CODE.md §5-6).

Namespaces are load-bearing, not cosmetic: they are how scoped subagent tool
sets are expressed and how search ranking stays legible (§5.4). The convention
is enforced at class-definition time — an unknown namespace, an un-namespaced
name, or untyped I/O is a developer error caught the moment the class is
created, not a runtime surprise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import ValidationError

from limpiador.observability.errors import MalformedInputError
from limpiador.schemas import Schema

# The enforced namespaces. ``git/github/fs/ast/test`` are the five surfaces the
# convention is stated in terms of; ``ci`` accompanies ``test`` for the
# CI-trigger tools (ARCHITECTURE.md §5.3, "test.* / ci.*"). A tool whose
# namespace is not here cannot be defined.
NAMESPACES: tuple[str, ...] = ("git", "github", "fs", "ast", "test", "ci")

# OpenAI function names disallow ".", so a namespaced tool name is transformed
# for the wire. The registry maps the transformed name back to the tool; the
# transform never needs to be reversed by string-parsing.
_NAME_SEPARATOR = "."
_OPENAI_NAME_SEPARATOR = "_"


class Tool(ABC):
    """Abstract base for every tool in the registry.

    Concrete tools set four class attributes — :attr:`name` (``<namespace>.<tool>``),
    :attr:`description`, :attr:`Input`, :attr:`Output` — and implement
    :meth:`run`. The namespace convention and typed-I/O contract are validated
    in ``__init_subclass__`` so a misdefined tool fails at import, not in a run.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    Input: ClassVar[type[Schema]]
    Output: ClassVar[type[Schema]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Intermediate abstract bases that do not declare a name are not tools;
        # only validate classes that actually claim a tool name.
        if "name" not in cls.__dict__:
            return
        _validate_tool_definition(cls)

    @classmethod
    def namespace(cls) -> str:
        """The tool's namespace — the part of its name before the dot."""
        return cls.name.split(_NAME_SEPARATOR, 1)[0]

    @classmethod
    def openai_name(cls) -> str:
        """The OpenAI-safe function name (function names may not contain dots)."""
        return cls.name.replace(_NAME_SEPARATOR, _OPENAI_NAME_SEPARATOR)

    @classmethod
    def openai_schema(cls) -> dict[str, object]:
        """Emit this tool as an OpenAI function-calling schema.

        The parameters are the input model's JSON schema, which carries the
        strict ``additionalProperties: false`` from :class:`Schema`.
        """
        return {
            "type": "function",
            "function": {
                "name": cls.openai_name(),
                "description": cls.description,
                "parameters": cls.Input.model_json_schema(),
            },
        }

    def invoke(self, request: Schema | dict[str, object]) -> Schema:
        """Run the tool with its I/O contract enforced at call time.

        This is the entry point the loop uses: it coerces/validates the request
        into the declared :attr:`Input`, calls :meth:`run`, and verifies the
        result is the declared :attr:`Output`. ``run`` therefore always receives
        a valid ``Input`` and is held to returning an ``Output`` — the
        definition-time check (Input/Output are Schemas) is not enough on its own.
        """
        typed_request = self._coerce_input(request)
        result = self.run(typed_request)
        self._check_output(result)
        return result

    def _coerce_input(self, request: Schema | dict[str, object]) -> Schema:
        """Return the request as a validated :attr:`Input`, or raise typed error."""
        if isinstance(request, self.Input):
            return request
        if isinstance(request, dict):
            try:
                return self.Input.model_validate(request)
            except ValidationError as error:
                raise MalformedInputError(
                    f"{self.name}: arguments do not satisfy {self.Input.__name__}: {error}"
                ) from error
        raise MalformedInputError(
            f"{self.name} expected {self.Input.__name__} or a dict, "
            f"got {type(request).__name__}."
        )

    def _check_output(self, result: object) -> None:
        """Enforce that :meth:`run` honored its :attr:`Output` contract."""
        if not isinstance(result, self.Output):
            raise TypeError(
                f"{self.name}.run() must return {self.Output.__name__}, "
                f"got {type(result).__name__}."
            )

    @abstractmethod
    def run(self, request: Schema) -> Schema:
        """Execute the tool's single action and return its typed output."""


def _validate_tool_definition(cls: type[Tool]) -> None:
    """Enforce the namespace convention and typed-I/O contract on a tool class."""
    _require_namespaced_name(cls)
    _require_non_empty_description(cls)
    _require_typed_io(cls)


def _require_namespaced_name(cls: type[Tool]) -> None:
    """The name must be exactly ``<namespace>.<tool>`` with a known namespace."""
    parts = cls.name.split(_NAME_SEPARATOR)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Tool name {cls.name!r} must be '<namespace>.<tool>' with a single dot."
        )
    if parts[0] not in NAMESPACES:
        raise ValueError(
            f"Tool {cls.name!r} uses unknown namespace {parts[0]!r}; "
            f"allowed namespaces are {NAMESPACES}."
        )


def _require_non_empty_description(cls: type[Tool]) -> None:
    """search_tools ranks on the description, so it cannot be blank."""
    description = getattr(cls, "description", "")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"Tool {cls.name!r} must set a non-empty description (search_tools ranks on it)."
        )


def _require_typed_io(cls: type[Tool]) -> None:
    """Input and Output must each be a limpiador :class:`Schema` subclass."""
    for attribute in ("Input", "Output"):
        model = getattr(cls, attribute, None)
        if not (isinstance(model, type) and issubclass(model, Schema)):
            raise ValueError(
                f"Tool {cls.name!r} must set {attribute} to a limpiador Schema subclass."
            )

"""Unit tests for the typed error hierarchy (CLEAN_CODE.md §6, Property 4).

Failures in limpiador are typed exceptions that serialize to a structured,
model-readable result — never a bare string, a ``None`` sentinel, or a swallowed
stack trace. These tests pin three things:

* each error type serializes via ``as_tool_result()`` to the expected payload
  (error name, message, and the recoverable/retryable flags the loop reasons on);
* the recoverable tool failures all descend from ``ToolError`` while
  ``ConfigError`` deliberately does not (it is an operator-fix, not agent-fold);
* a source-level guard: no bare/broad ``except`` and no exception swallowed by a
  sentinel return anywhere in ``src/``.
"""

from __future__ import annotations

import pathlib

from limpiador.observability.errors import (
    ConfigError,
    MalformedInputError,
    NotFoundError,
    PermissionDeniedError,
    ToolError,
    ToolUnavailableError,
    TransientError,
)

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "limpiador"


# ---- serialization ----------------------------------------------------------
def test_not_found_serializes_to_the_expected_structured_result() -> None:
    result = NotFoundError("no such file: billing.py").as_tool_result()
    assert result == {
        "error": "NotFoundError",
        "message": "no such file: billing.py",
        "recoverable": True,
        "retryable": False,
    }


def test_a_transient_error_is_marked_retryable() -> None:
    result = TransientError("rate limited, try again").as_tool_result()
    assert result["error"] == "TransientError"
    assert result["recoverable"] is True
    assert result["retryable"] is True


def test_every_tool_error_serializes_with_its_own_class_name() -> None:
    for cls in (
        NotFoundError,
        PermissionDeniedError,
        TransientError,
        MalformedInputError,
        ToolUnavailableError,
    ):
        payload = cls("boom").as_tool_result()
        assert payload["error"] == cls.__name__
        assert payload["message"] == "boom"
        assert payload["recoverable"] is True  # every tool failure is foldable


def test_config_error_serializes_as_non_recoverable() -> None:
    result = ConfigError("OPENAI_API_KEY is not set").as_tool_result()
    assert result["error"] == "ConfigError"
    assert result["recoverable"] is False  # the operator must fix it; the agent cannot


# ---- hierarchy --------------------------------------------------------------
def test_tool_failures_descend_from_tool_error() -> None:
    for cls in (
        NotFoundError,
        PermissionDeniedError,
        TransientError,
        MalformedInputError,
        ToolUnavailableError,
    ):
        assert issubclass(cls, ToolError)


def test_config_error_is_not_a_tool_error() -> None:
    """It is a startup misconfiguration, not an agent-recoverable tool failure."""
    assert not issubclass(ConfigError, ToolError)
    # but it still carries the structured, model-readable payload contract
    assert hasattr(ConfigError("x"), "as_tool_result")


# ---- source-level guards (CLEAN_CODE.md §6) ---------------------------------
def _src_files() -> list[pathlib.Path]:
    return list(_SRC_ROOT.rglob("*.py"))


def test_src_has_no_bare_or_broad_excepts() -> None:
    offenders: list[str] = []
    for path in _src_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped == "except:" or stripped.startswith("except :"):
                offenders.append(f"{path.name}:{lineno} bare except")
            if stripped.startswith(("except Exception", "except BaseException")):
                offenders.append(f"{path.name}:{lineno} broad except")
    assert offenders == [], f"failures must be caught by type, not broadly: {offenders}"


def test_src_never_swallows_an_exception_with_a_sentinel() -> None:
    """No ``except`` block silently swallows via ``pass`` or a sentinel return."""
    swallow = {"pass", "return", "return None", "return False"}
    offenders: list[str] = []
    for path in _src_files():
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not (stripped.startswith("except") and stripped.endswith(":")):
                continue
            for nxt in lines[i + 1 :]:
                body = nxt.strip()
                if not body or body.startswith("#"):
                    continue
                if body in swallow:
                    offenders.append(f"{path.name}:{i + 1} swallows via {body!r}")
                break
    assert offenders == [], f"exceptions must be folded or re-raised, never swallowed: {offenders}"

"""Shared result and error models.

The result envelope follows the pattern used by the reference Daofy server.  Keeping
the envelope in one place makes all MCP tools predictable for an AI client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class AndroidMcpError(Exception):
    """An expected, user-actionable error returned by a tool."""

    def __init__(self, message: str, *, code: str = "invalid_request", hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def ok(data: Any = None, *, hint: str | None = None, **meta: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "data": data,
        "error": None,
        "meta": {"timestamp": now_iso(), **meta},
    }
    if hint:
        result["hint"] = hint
    return result


def fail(error: AndroidMcpError | str, *, code: str | None = None, hint: str | None = None) -> dict[str, Any]:
    if isinstance(error, AndroidMcpError):
        message = error.message
        error_code = error.code
        error_hint = error.hint
    else:
        message = error
        error_code = code or "tool_error"
        error_hint = None
    return {
        "success": False,
        "data": None,
        "error": {"code": error_code, "message": message, "hint": error_hint or hint},
        "meta": {"timestamp": now_iso()},
    }


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    plugin: str
    actions: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    handler: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)

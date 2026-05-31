"""A tiny built-in tool, used to exercise the agentic loop before MCP arrives (A1)."""

from __future__ import annotations

from typing import Any

from .base import Tool


async def _add(args: dict[str, Any]) -> str:
    return str(int(args["a"]) + int(args["b"]))


add_tool = Tool(
    name="add",
    description="Add two integers and return the sum.",
    schema={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
    call=_add,
)

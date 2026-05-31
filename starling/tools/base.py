"""Tool abstraction + registry.

A ``Tool`` is a named, schema-described async callable a worker can invoke. A
``ToolRegistry`` holds the tools a given role may use and exposes them as OpenAI
function-calling definitions. MCP servers (Phase A2) become Tools via this same
interface. See ARCHITECTURE.md §8.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# An async tool implementation: takes parsed JSON-schema args, returns a text result.
ToolCall = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema for the arguments
    call: ToolCall

    def openai_def(self) -> dict[str, Any]:
        """Render as an OpenAI function-tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


class ToolRegistry:
    """The set of tools available to one task, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def openai_defs(self) -> list[dict[str, Any]]:
        return [tool.openai_def() for tool in self._tools.values()]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"error: unknown tool {name!r}"
        return await tool.call(args)


# Verbs that mark a tool as state-changing / sensitive — these need human approval
# (Phase B). Until then, only read-only tools are exposed to workers.
_SENSITIVE_HINTS = (
    "create", "update", "delete", "write", "edit", "move", "remove",
    "rename", "send", "push", "append", "insert", "upload", "merge",
)


def is_sensitive(tool_name: str) -> bool:
    """Heuristic: does this tool change external state (and thus need approval)?"""
    op = tool_name.split("__", 1)[-1].lower()
    return any(hint in op for hint in _SENSITIVE_HINTS)

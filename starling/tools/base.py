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
    force_safe: bool = False  # True => always auto-run (e.g. a search/fetch server)

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

    def is_safe(self, name: str) -> bool:
        """Whether a tool can auto-run (no approval needed)."""
        tool = self._tools.get(name)
        return tool_is_safe(tool) if tool is not None else is_read_only(name)


# Read-only prefixes — only tools whose operation starts with one of these are exposed
# to workers until the approval layer (Phase B) can gate state-changing actions. This is
# default-deny: writes (create/update/delete/push/fork/merge/...) stay out by omission,
# which is safer than trying to enumerate every write verb.
_READ_PREFIXES = (
    "get", "list", "search", "read", "fetch", "find", "describe", "show",
    "query", "directory_tree",
)


def is_read_only(tool_name: str) -> bool:
    """Heuristic: is this tool safe to auto-run (no external state change)?"""
    op = tool_name.split("__", 1)[-1].lower()
    return op.startswith(_READ_PREFIXES)


def tool_is_safe(tool: Tool) -> bool:
    """Safe (auto-run) if its server forced it so, or its name reads as read-only.

    The override matters for search/fetch servers whose tool names (e.g.
    ``brave_web_search``) don't begin with a read verb but are still read-only.
    """
    return tool.force_safe or is_read_only(tool.name)

"""MCP client manager — connect to MCP servers and expose their tools as Tools.

Reads ``mcp_servers.json`` (Claude-Desktop format), launches/connects each server over
stdio, discovers its tools, and wraps each as a Starling ``Tool`` (namespaced
``server__tool``). One client, many pre-built integrations. The long-lived sessions are
held open for the app's lifetime via an ``AsyncExitStack``. See ARCHITECTURE.md §8.2.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .base import Tool, ToolRegistry, tool_is_safe

DEFAULT_CONFIG_PATH = "mcp_servers.json"


def _content_text(result: Any) -> str:
    """Flatten an MCP CallToolResult's content blocks into a single string."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts).strip()


class MCPManager:
    """Connects to configured MCP servers and exposes their tools as Starling Tools."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, Tool] = {}

    async def start(self, config: dict[str, Any] | None = None) -> None:
        """Connect every server in the config and discover its tools.

        One server failing to connect (e.g. a missing token) is logged and skipped, so
        it never takes down the others.
        """
        if config is None:
            with open(self._config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        for name, spec in config.get("mcpServers", {}).items():
            if name.startswith("_"):  # skip example/commented entries
                continue
            try:
                await self._connect(name, spec)
            except Exception as exc:
                print(f"[mcp] failed to connect '{name}': {exc}")

    async def _connect(self, name: str, spec: dict[str, Any]) -> None:
        env = {**os.environ, **spec["env"]} if spec.get("env") else None
        params = StdioServerParameters(
            command=spec["command"], args=spec.get("args", []), env=env
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session
        read_only = bool(spec.get("read_only", False))  # per-server "trust as safe" override
        tools_result = await session.list_tools()
        for mcp_tool in tools_result.tools:
            tool = self._wrap(name, session, mcp_tool, read_only)
            self._tools[tool.name] = tool
        print(f"[mcp] connected '{name}': {len(tools_result.tools)} tools")

    @staticmethod
    def _wrap(server: str, session: ClientSession, mcp_tool: Any, read_only: bool = False) -> Tool:
        async def _call(args: dict[str, Any]) -> str:
            return _content_text(await session.call_tool(mcp_tool.name, args))

        return Tool(
            name=f"{server}__{mcp_tool.name}",
            description=mcp_tool.description or "",
            schema=mcp_tool.inputSchema or {"type": "object", "properties": {}},
            call=_call,
            force_safe=read_only,
        )

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def registry_for(self, servers: list[str], include_sensitive: bool = False) -> ToolRegistry:
        """A ToolRegistry of the named servers' tools.

        ``include_sensitive`` stays False until the approval layer (Phase B) exists, so
        workers only get read-only tools and can't write/delete anything unguarded.
        """
        registry = ToolRegistry()
        for tool in self._tools.values():
            if tool.name.split("__", 1)[0] not in servers:
                continue
            if not include_sensitive and not tool_is_safe(tool):
                continue
            registry.add(tool)
        return registry

    async def aclose(self) -> None:
        await self._stack.aclose()

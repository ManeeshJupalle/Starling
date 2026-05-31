"""Phase D verification: web tools + the per-server read_only override.

A search tool like `web__brave_web_search` doesn't begin with a read verb, so the
name heuristic alone would wrongly hide it / gate it for approval. The per-server
`read_only: true` override marks it force_safe, so it's exposed and auto-runs.

Offline (the live "search the web" check needs a real BRAVE_API_KEY in mcp_servers.json).
"""

import asyncio

from scratch_fakes import chat, text_response, tool_response
from starling.agents.roles import ROLE_TOOLS
from starling.agents.worker import run_task
from starling.tools.base import Tool, ToolRegistry, tool_is_safe
from starling.tools.mcp import MCPManager


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class SequenceClient:
    def __init__(self, responses) -> None:
        self.chat = chat(self._create)
        self._responses = list(responses)
        self.calls = 0

    async def _create(self, **kw):
        self.calls += 1
        return self._responses.pop(0)


class _FakeMcpTool:
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


def search_tool(force_safe: bool):
    async def _search(args):
        return "results"
    return Tool("web__brave_web_search", "Web search", {"type": "object"}, _search, force_safe=force_safe)


def test_safety_override() -> None:
    print("force_safe overrides the name heuristic:")
    check("search tool name is NOT read-only by heuristic", not tool_is_safe(search_tool(False)))
    check("force_safe makes it safe", tool_is_safe(search_tool(True)))

    reg = ToolRegistry()
    reg.add(search_tool(True))
    check("registry.is_safe respects force_safe", reg.is_safe("web__brave_web_search"))


def test_wrap_applies_read_only() -> None:
    print("\n_wrap applies the per-server read_only flag:")
    tool = MCPManager._wrap("web", None, _FakeMcpTool("brave_web_search", "Search", {}), read_only=True)
    check("namespaced name", tool.name == "web__brave_web_search")
    check("force_safe set from read_only", tool.force_safe is True)


async def test_worker_autoruns_safe_search() -> None:
    print("\nworker auto-runs a forced-safe search tool (no approval pause):")
    ran = []

    async def _search(args):
        ran.append(args)
        return "search results about X"

    reg = ToolRegistry()
    reg.add(Tool("web__brave_web_search", "Web search", {"type": "object"}, _search, force_safe=True))
    client = SequenceClient([
        tool_response("web__brave_web_search", {"q": "latest news"}),
        text_response("Here's what I found."),
    ])
    result = await run_task("researcher", "what's new?", client=client, tools=reg, allow_sensitive=True)
    check("search executed (not paused, despite allow_sensitive)", len(ran) == 1)
    check("worker finished with an answer", result.done and result.output == "Here's what I found.")


async def test_worker_pauses_without_override() -> None:
    print("\nwithout the override, the same tool would pause for approval:")
    reg = ToolRegistry()
    reg.add(search_tool(False))  # force_safe=False
    client = SequenceClient([tool_response("web__brave_web_search", {"q": "x"})])
    result = await run_task("researcher", "q", client=client, tools=reg, allow_sensitive=True)
    check("worker pauses (no override -> treated as sensitive)", not result.done)


def test_role_grant() -> None:
    print("\nresearcher granted the web server:")
    check("researcher -> filesystem + github + web", ROLE_TOOLS["researcher"] == ["filesystem", "github", "web"])


async def main() -> None:
    test_safety_override()
    test_wrap_applies_read_only()
    await test_worker_autoruns_safe_search()
    await test_worker_pauses_without_override()
    test_role_grant()
    print("\nALL PASS: web tools + per-server read_only override (Phase D).")


if __name__ == "__main__":
    asyncio.run(main())

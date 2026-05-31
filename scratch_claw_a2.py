"""Scratch verification for Phase A2 (MCP manager).

Two parts:
  - offline: verify the MCP-tool -> Starling-Tool wrapping (namespacing, schema,
    call routing, content flattening) with a fake session — no Node needed.
  - live: start the MCPManager against the real filesystem server and print the
    discovered tools. Requires Node.js (npx) and network on first run.
"""

import asyncio

from starling.tools.mcp import MCPManager, _content_text


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


# --- offline: wrapping logic ----------------------------------------------

class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _CallResult:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        return _CallResult(f"RESULT({name})")


class FakeMcpTool:
    def __init__(self, name, description, inputSchema) -> None:
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


async def test_wrapping() -> None:
    print("offline: MCP tool -> Starling Tool wrapping:")
    session = FakeSession()
    mcp_tool = FakeMcpTool(
        "read_file", "Read a file",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    tool = MCPManager._wrap("filesystem", session, mcp_tool)

    check("namespaced name", tool.name == "filesystem__read_file")
    check("description carried over", tool.description == "Read a file")
    check("input schema preserved", tool.schema == mcp_tool.inputSchema)
    check("renders an OpenAI function def", tool.openai_def()["function"]["name"] == "filesystem__read_file")

    out = await tool.call({"path": "a.txt"})
    check("call routed with the un-namespaced tool name", session.calls == [("read_file", {"path": "a.txt"})])
    check("text content flattened", out == "RESULT(read_file)")


def test_content_text() -> None:
    print("\noffline: content flattening:")
    result = _CallResult("hello")
    result.content.append(_Block("world"))
    check("joins text blocks", _content_text(result) == "hello\nworld")
    check("empty content -> ''", _content_text(_CallResult("")) == "")


# --- live: connect the real filesystem server -----------------------------

async def test_live() -> None:
    print("\nlive: connect the filesystem server + list tools (needs Node/npx):")
    manager = MCPManager("mcp_servers.json")
    try:
        await manager.start()
        names = sorted(t.name for t in manager.tools())
        for n in names:
            print(f"   - {n}")
        check("filesystem tools discovered", any(n.startswith("filesystem__") for n in names))
        check("read_file is available", "filesystem__read_file" in names)
    finally:
        await manager.aclose()


async def main() -> None:
    await test_wrapping()
    test_content_text()
    await test_live()
    print("\nALL PASS: MCP manager connects, discovers, and wraps tools (A2).")


if __name__ == "__main__":
    asyncio.run(main())

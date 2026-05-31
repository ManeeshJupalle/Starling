"""A3 verification: per-role MCP tools wired into the worker.

Uses the REAL filesystem MCP server but a FAKE (scripted) LLM client, so it proves the
wiring + a real MCP file read end-to-end without Telegram or LLM tokens:
  classify -> ephemeral(summarizer) -> the worker loop calls filesystem__read_file on
  the real workspace/README.md -> the file content flows into the final reply.
Also checks the read-only filter hides the filesystem write tools.
"""

import asyncio
import os

from scratch_fakes import FakeChannel, chat, text_response, tool_name, tool_response
from starling.agents.roles import tools_for_role
from starling.blackboard import Blackboard
from starling.orchestrator import Orchestrator
from starling.tools.mcp import MCPManager

README_PATH = os.path.abspath(os.path.join("workspace", "README.md"))


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class ScriptedClient:
    """classify -> ephemeral(summarizer); the summarizer loop reads the file, then summarizes."""

    def __init__(self) -> None:
        self.chat = chat(self._create)
        self.steps: list[str] = []

    async def _create(self, **kw):
        if tool_name(kw) == "classify_request":
            self.steps.append("classify")
            return tool_response("classify_request", {
                "mode": "ephemeral",
                "goal": "summarize the README in my workspace",
                "workers": ["summarizer"],
            })
        messages = kw["messages"]
        if not any(m.get("role") == "tool" for m in messages):
            self.steps.append("tool_call")
            return tool_response("filesystem__read_file", {"path": README_PATH})
        self.steps.append("final")
        tool_text = next(m["content"] for m in messages if m.get("role") == "tool")
        return text_response("SUMMARY of workspace README >>> " + tool_text[:60])


def test_read_only_filter(manager: MCPManager) -> None:
    print("read-only filter on the per-role registry:")
    names = tools_for_role(manager, "summarizer").names()
    check("read_file exposed", "filesystem__read_file" in names)
    check("write_file NOT exposed (no approval layer yet)", "filesystem__write_file" not in names)
    check("edit_file NOT exposed", "filesystem__edit_file" not in names)
    check("move_file NOT exposed", "filesystem__move_file" not in names)


async def test_end_to_end(manager: MCPManager) -> None:
    print("\nend-to-end: summarize the README via a real MCP read:")
    client = ScriptedClient()
    channel = FakeChannel()
    orch = Orchestrator(channel, client, Blackboard(":memory:"), tools_manager=manager)
    await orch.handle_message(7, "summarize the README in my workspace")

    check("classified -> called a tool -> answered", client.steps == ["classify", "tool_call", "final"])
    check("one reply sent", len(channel.sent) == 1)
    reply = channel.sent[0][1]
    check("reply is the summary", reply.startswith("SUMMARY of workspace README"))
    check("real file content flowed through", "Starling workspace" in reply)


async def main() -> None:
    manager = MCPManager("mcp_servers.json")
    try:
        await manager.start()
        test_read_only_filter(manager)
        await test_end_to_end(manager)
    finally:
        await manager.aclose()
    print("\nALL PASS: per-role MCP tools wired into the worker, read-only (A3).")


if __name__ == "__main__":
    asyncio.run(main())

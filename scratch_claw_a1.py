"""Scratch verification for Phase A1 (agentic tool-use loop, no MCP yet).

Drives run_task's tool loop with a fake client and a real ToolRegistry: the model
"calls" a tool, the loop executes it and feeds the result back, then the model returns
a final answer. Also checks the no-tools path still collapses to a single call -> text
(so the existing project flow is unchanged).
"""

import asyncio

from scratch_fakes import chat, text_response, tool_response
from starling.agents.worker import run_task
from starling.tools.base import Tool, ToolRegistry
from starling.tools.builtin import add_tool


class SequenceClient:
    """Returns a scripted list of chat responses on successive create() calls."""

    def __init__(self, responses) -> None:
        self.chat = chat(self._create)
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict = {}

    async def _create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        return self._responses.pop(0)


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


async def test_tool_loop() -> None:
    print("tool-use loop (model calls a tool, result fed back, final answer):")
    seen: list[dict] = []

    async def add_spy(args):
        seen.append(args)
        return await add_tool.call(args)

    registry = ToolRegistry()
    registry.add(Tool("add", add_tool.description, add_tool.schema, add_spy))

    client = SequenceClient([
        tool_response("add", {"a": 2, "b": 3}),  # round 1: model calls the tool
        text_response("The answer is 5."),        # round 2: model gives the final answer
    ])
    out = await run_task("researcher", "What is 2+3? Use the add tool.", client=client, tools=registry)

    check("tool executed once", len(seen) == 1)
    check("tool received the parsed args", seen[0] == {"a": 2, "b": 3})
    check("two model rounds (tool call + final)", client.calls == 2)
    check("tools were offered to the model", "tools" in client.last_kwargs)
    check("final answer returned", out == "The answer is 5.")


async def test_builtin_add() -> None:
    print("\nbuilt-in add tool + registry:")
    check("add(2,3) -> '5'", await add_tool.call({"a": 2, "b": 3}) == "5")
    reg = ToolRegistry()
    reg.add(add_tool)
    check("registry renders an OpenAI function def",
          reg.openai_defs()[0]["function"]["name"] == "add")
    check("unknown tool returns an error string", "unknown tool" in await reg.call("nope", {}))


async def test_no_tools_backcompat() -> None:
    print("\nno-tools path (unchanged single call -> text):")
    client = SequenceClient([text_response("hello world")])
    out = await run_task("summarizer", "say hi", client=client)
    check("one model call", client.calls == 1)
    check("no tools offered when none given", "tools" not in client.last_kwargs)
    check("returns the text", out == "hello world")


async def main() -> None:
    await test_tool_loop()
    await test_builtin_add()
    await test_no_tools_backcompat()
    print("\nALL PASS: agentic tool-use loop verified offline (A1).")


if __name__ == "__main__":
    asyncio.run(main())

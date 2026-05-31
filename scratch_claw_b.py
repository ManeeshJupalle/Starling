"""Phase B verification: sensitive actions gated by human approval.

Drives the real scheduler + blackboard + orchestrator with a fake LLM client and a
fake sensitive tool (no Telegram/MCP). A project task's worker tries a write tool ->
the task pauses (awaiting_human) and asks -> the user's reply is routed to it ->
on "yes" the write executes and the project completes; on "no" it's skipped.
"""

import asyncio

from scratch_fakes import FakeChannel, chat, text_response, tool_response
from starling.blackboard import Blackboard, TaskStatus
from starling.orchestrator import Orchestrator
from starling.scheduler import Scheduler
from starling.tools.base import Tool, ToolRegistry, is_read_only


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class FakeManager:
    """Exposes a read tool and a sensitive write tool (mimics MCPManager.registry_for)."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

        async def _read(args):
            return "FILE CONTENTS"

        async def _write(args):
            self.writes.append(args)
            return f"wrote {args.get('path')}"

        self._tools = {
            "filesystem__read_file": Tool("filesystem__read_file", "Read", {"type": "object"}, _read),
            "filesystem__write_file": Tool("filesystem__write_file", "Write", {"type": "object"}, _write),
        }

    def registry_for(self, servers, include_sensitive=False) -> ToolRegistry:
        reg = ToolRegistry()
        for name, tool in self._tools.items():
            if include_sensitive or is_read_only(name):
                reg.add(tool)
        return reg


class SequenceClient:
    def __init__(self, responses) -> None:
        self.chat = chat(self._create)
        self._responses = list(responses)
        self.calls = 0

    async def _create(self, **kw):
        self.calls += 1
        return self._responses.pop(0)


async def run_scenario(reply: str):
    bb = Blackboard(":memory:")
    manager = FakeManager()
    channel = FakeChannel()
    client = SequenceClient([
        tool_response("filesystem__write_file", {"path": "out.txt", "content": "hi"}),
        text_response("All done."),
    ])
    scheduler = Scheduler(bb, channel, client, tick_interval=0.01, tools_manager=manager)
    orch = Orchestrator(channel, client, bb, scheduler, tools_manager=manager)

    pid = bb.create_project(7, "make a file")
    tid = bb.add_task("researcher", "create out.txt containing 'hi'", project_id=pid)

    await scheduler.tick()            # worker wants write_file (sensitive) -> pauses
    paused = bb.get_task(tid)
    await orch.handle_message(7, reply)  # route the approval reply to the paused task
    final = bb.get_task(tid)
    return manager, channel, paused, final


async def test_pause_and_ask() -> None:
    print("worker pauses on a sensitive tool and asks for approval:")
    manager, channel, paused, _ = await run_scenario("yes")
    check("task paused awaiting_human", paused["status"] == TaskStatus.AWAITING_HUMAN)
    check("checkpoint stored on the task", paused["checkpoint"] is not None)
    check("approval asked in chat", any("Approve?" in t for t in channel.texts()))
    # (state captured before the reply was processed by run_scenario)


async def test_approve() -> None:
    print("\napprove -> the write executes and the project completes:")
    manager, channel, _, final = await run_scenario("yes")
    check("write executed after approval", manager.writes == [{"path": "out.txt", "content": "hi"}])
    check("task done", final["status"] == TaskStatus.DONE)
    check("project completion posted", any("complete" in t.lower() for t in channel.texts()))


async def test_deny() -> None:
    print("\ndeny -> the write is skipped, the agent finishes without it:")
    manager, channel, _, final = await run_scenario("no")
    check("write NOT executed on deny", manager.writes == [])
    check("task still completes (agent adapts)", final["status"] == TaskStatus.DONE)


async def main() -> None:
    await test_pause_and_ask()
    await test_approve()
    await test_deny()
    print("\nALL PASS: sensitive actions gated by human approval (Phase B).")


if __name__ == "__main__":
    asyncio.run(main())

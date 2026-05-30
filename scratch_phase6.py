"""Scratch verification for Phase 6 (human-in-the-loop).

Runs a project containing a decision point end-to-end with a fake Anthropic client
and a real file-backed blackboard, so no API key or Telegram token is needed. Covers:
the PM emitting a 'pm' question task, the scheduler pausing it (awaiting_human + ask),
the orchestrator routing the next reply to the waiting task (not classifying it as a
new request), the answer flowing to downstream tasks, and the project finishing.

The live check (start a decision-point project via Telegram, reply, watch it resume)
is run separately with real keys via ``python -m starling``.
"""

import asyncio
import os

from starling.agents.roles import ROLE_PROMPTS
from starling.blackboard import Blackboard, TaskStatus
from starling.channels.base import Channel, InboundHandler
from starling.orchestrator import Orchestrator
from starling.scheduler import Scheduler

DB = "phase6_scratch.db"
_SYS_TO_ROLE = {prompt: role for role, prompt in ROLE_PROMPTS.items()}

QUESTION = "Which city do you want to visit - Paris or Rome?"
CLASSIFICATION = {"mode": "project", "goal": "plan a weekend trip", "workers": []}
PLAN = {"tasks": [
    {"role": "pm", "description": QUESTION, "depends_on": []},
    {"role": "researcher", "description": "research top activities in the chosen city", "depends_on": [0]},
    {"role": "summarizer", "description": "write a short itinerary", "depends_on": [1]},
]}


# --- fakes ----------------------------------------------------------------

class FakeChannel(Channel):
    def __init__(self) -> None:
        self._handler: InboundHandler | None = None
        self.sent: list[tuple[int, str]] = []

    def on_message(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def run(self, on_start=None) -> None:
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [t for _, t in self.sent]


class _Block:
    def __init__(self, type: str, text: str | None = None, input: dict | None = None) -> None:
        self.type = type
        self.text = text
        self.input = input


class _Resp:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content


class _Messages:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    async def create(self, **kw):
        return await self._client._create(**kw)


class FakeClient:
    def __init__(self) -> None:
        self.messages = _Messages(self)
        self.calls: list[str] = []                  # classify | decompose
        self.worker_prompts: list[tuple[str, str]] = []  # (role, user)

    async def _create(self, **kw):
        tools = kw.get("tools") or []
        name = tools[0]["name"] if tools else ""
        if name == "classify_request":
            self.calls.append("classify")
            return _Resp([_Block("tool_use", input=CLASSIFICATION)])
        if name == "submit_plan":
            self.calls.append("decompose")
            return _Resp([_Block("tool_use", input=PLAN)])
        role = _SYS_TO_ROLE.get(kw["system"], "?")
        user = kw["messages"][0]["content"]
        self.worker_prompts.append((role, user))
        if role == "researcher":
            return _Resp([_Block("text", text=f"RDATA({user})")])
        if role == "summarizer":
            return _Resp([_Block("text", text="ITINERARY")])
        return _Resp([_Block("text", text="X")])

    def classify_count(self) -> int:
        return self.calls.count("classify")

    def worker_count(self, role: str) -> int:
        return sum(1 for r, _ in self.worker_prompts if r == role)


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


async def drain(sched: Scheduler, bb: Blackboard, pid: int, max_ticks: int = 10) -> None:
    for _ in range(max_ticks):
        await sched.tick()
        tasks = bb.project_tasks(pid)
        if all(t["status"] in (TaskStatus.DONE, TaskStatus.FAILED) for t in tasks):
            return


async def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)
    bb = Blackboard(DB)
    channel = FakeChannel()
    client = FakeClient()
    sched = Scheduler(bb, channel, client, tick_interval=0.01)
    orch = Orchestrator(channel, client, bb, sched)
    chat = 7

    print("1. user starts a project with a decision point:")
    await orch.handle_message(chat, "plan me a weekend trip")
    pid = 1
    check("project created with 3 tasks", len(bb.project_tasks(pid)) == 3)
    check("classified once (no paused task yet)", client.classify_count() == 1)
    check("start confirmation sent", any("Project #1" in t for t in channel.texts()))

    print("\n2. scheduler reaches the pm task and pauses for a decision:")
    await sched.tick()
    pm_task = bb.get_task(1)
    check("pm task is awaiting_human", pm_task["status"] == TaskStatus.AWAITING_HUMAN)
    check("question stored on the task", pm_task["question"] == QUESTION)
    check("question asked in the chat", QUESTION in channel.texts())
    check("no workers run while paused", client.worker_count("researcher") == 0)
    check("blackboard reports the paused task for this chat",
          bb.awaiting_human(chat) is not None and bb.awaiting_human(chat)["id"] == 1)

    print("\n3. user's reply is routed to the waiting task (not a new request):")
    await orch.handle_message(chat, "Paris")
    check("NO new classification for the reply", client.classify_count() == 1)
    pm_task = bb.get_task(1)
    check("pm task now done", pm_task["status"] == TaskStatus.DONE)
    check("answer stored as the task's output", pm_task["output"] == "Paris")
    check("reply acknowledged", any("continuing" in t.lower() for t in channel.texts()))

    print("\n4. project resumes and finishes, using the answer:")
    await drain(sched, bb, pid)
    tasks = {t["id"]: t for t in bb.project_tasks(pid)}
    check("all tasks done", all(t["status"] == TaskStatus.DONE for t in tasks.values()))
    check("pm task never run as a worker", client.worker_count("pm") == 0)
    check("researcher ran once", client.worker_count("researcher") == 1)
    research_prompt = next(u for r, u in client.worker_prompts if r == "researcher")
    check("researcher received the human's answer (Paris)", "Paris" in research_prompt)
    check("final itinerary posted to the chat", any("ITINERARY" in t for t in channel.texts()))
    bb.close()

    if os.path.exists(DB):
        os.remove(DB)
    print("\nALL PASS: human-in-the-loop pause, reply routing, and resume verified offline.")


if __name__ == "__main__":
    asyncio.run(main())

"""Scratch verification for Phase 5 (scheduler: heartbeat, deps, resume).

Drives a real file-backed blackboard project through the scheduler with a fake
Anthropic client, so no API key or Telegram token is needed. Covers: running a
dependency graph to completion, passing upstream outputs as inputs, posting only the
terminal task's result, crash recovery (running -> ready), and resuming after a
restart without redoing completed tasks.

The live check (run the task-queue project via Telegram, then kill + restart) is run
separately with real keys via ``python -m starling``.
"""

import asyncio
import os

from starling.agents.roles import ROLE_PROMPTS
from starling.blackboard import Blackboard, TaskStatus
from starling.channels.base import Channel, InboundHandler
from starling.scheduler import Scheduler

DB = "phase5_scratch.db"
_SYS_TO_ROLE = {prompt: role for role, prompt in ROLE_PROMPTS.items()}


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


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content


class _Messages:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    async def create(self, **kw):
        return await self._client._create(**kw)


class FakeClient:
    """Returns role-tagged worker output and records the prompt it was given."""

    def __init__(self) -> None:
        self.messages = _Messages(self)
        self.prompts: list[tuple[str, str]] = []  # (role, user_prompt)

    async def _create(self, **kw):
        system = kw["system"]
        user = kw["messages"][0]["content"]
        role = _SYS_TO_ROLE.get(system, "?")
        self.prompts.append((role, user))
        if role == "researcher":
            return _Resp([_Block(f"RDATA({user})")])
        if role == "summarizer":
            return _Resp([_Block("FINAL-COMPARISON")])
        return _Resp([_Block("X")])

    def role_calls(self, role: str) -> int:
        return sum(1 for r, _ in self.prompts if r == role)


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def build_project(bb: Blackboard) -> tuple[int, list[int], int]:
    """3 researchers -> 1 summarizer. Returns (project_id, researcher_ids, summarizer_id)."""
    pid = bb.create_project(chat_id=7, goal="compare the top 3 task queues")
    researchers = [
        bb.add_task("researcher", f"research {lib}", project_id=pid)
        for lib in ("Celery", "RQ", "Dramatiq")
    ]
    summarizer = bb.add_task(
        "summarizer", "write the comparison", project_id=pid, depends_on=researchers
    )
    return pid, researchers, summarizer


async def drain(sched: Scheduler, bb: Blackboard, pid: int, max_ticks: int = 10) -> None:
    for _ in range(max_ticks):
        await sched.tick()
        tasks = bb.project_tasks(pid)
        if all(t["status"] in (TaskStatus.DONE, TaskStatus.FAILED) for t in tasks):
            return


# --- checks ---------------------------------------------------------------

async def test_runs_to_completion() -> None:
    print("scheduler runs a dependency graph to completion:")
    if os.path.exists(DB):
        os.remove(DB)
    bb = Blackboard(DB)
    pid, researchers, summarizer = build_project(bb)
    channel = FakeChannel()
    client = FakeClient()
    sched = Scheduler(bb, channel, client, tick_interval=0.01)

    await drain(sched, bb, pid)

    tasks = {t["id"]: t for t in bb.project_tasks(pid)}
    check("all tasks done", all(t["status"] == TaskStatus.DONE for t in tasks.values()))
    check("3 researcher runs + 1 summarizer run",
          client.role_calls("researcher") == 3 and client.role_calls("summarizer") == 1)

    # The summarizer's prompt must contain the researchers' outputs (inputs passed).
    summ_prompt = next(user for role, user in client.prompts if role == "summarizer")
    check("summarizer received upstream researcher outputs", "RDATA(" in summ_prompt)
    check("summarizer output stored", tasks[summarizer]["output"] == "FINAL-COMPARISON")

    # Only the terminal task (summarizer) is reported, once, to the project's chat.
    check("exactly one result posted", len(channel.sent) == 1)
    check("posted to the project's chat", channel.sent[0][0] == 7)
    check("posted the final comparison", "FINAL-COMPARISON" in channel.sent[0][1])
    bb.close()


def test_recover_resets_running() -> None:
    print("\ncrash recovery requeues interrupted (running) tasks:")
    if os.path.exists(DB):
        os.remove(DB)
    bb = Blackboard(DB)
    pid = bb.create_project(chat_id=1, goal="g")
    tid = bb.add_task("researcher", "x", project_id=pid)
    bb.set_status(tid, TaskStatus.RUNNING)  # simulate a task in flight at crash time
    sched = Scheduler(bb, FakeChannel(), FakeClient(), tick_interval=0.01)

    count = sched.recover()
    check("one running task requeued", count == 1)
    check("task is back to ready", bb.get_task(tid)["status"] == TaskStatus.READY)
    bb.close()


async def test_resume_skips_completed() -> None:
    print("\nrestart resumes without redoing completed tasks:")
    if os.path.exists(DB):
        os.remove(DB)

    # First process: run one tick so the researchers finish, then "crash".
    bb1 = Blackboard(DB)
    pid, researchers, summarizer = build_project(bb1)
    client1 = FakeClient()
    await Scheduler(bb1, FakeChannel(), client1, tick_interval=0.01).tick()
    check("researchers done before crash", all(
        bb1.get_task(r)["status"] == TaskStatus.DONE for r in researchers))
    check("summarizer not yet run", bb1.get_task(summarizer)["status"] == TaskStatus.PENDING)
    check("3 researcher runs in first process", client1.role_calls("researcher") == 3)
    bb1.close()

    # Second process: reopen the same .db with a fresh client + channel.
    bb2 = Blackboard(DB)
    client2 = FakeClient()
    channel2 = FakeChannel()
    sched2 = Scheduler(bb2, channel2, client2, tick_interval=0.01)
    sched2.recover()
    await drain(sched2, bb2, pid)

    check("researchers were NOT re-run after restart", client2.role_calls("researcher") == 0)
    check("summarizer ran once after restart", client2.role_calls("summarizer") == 1)
    check("project finished", bb2.get_task(summarizer)["status"] == TaskStatus.DONE)
    check("summarizer used persisted researcher outputs",
          "RDATA(" in next(u for r, u in client2.prompts if r == "summarizer"))
    check("final result posted after resume", any("FINAL-COMPARISON" in t for _, t in channel2.sent))
    bb2.close()


async def main() -> None:
    await test_runs_to_completion()
    test_recover_resets_running()
    await test_resume_skips_completed()
    if os.path.exists(DB):
        os.remove(DB)
    print("\nALL PASS: scheduler completion, dependency inputs, recovery, and resume verified.")


if __name__ == "__main__":
    asyncio.run(main())

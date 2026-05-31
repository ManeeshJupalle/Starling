"""Scratch verification for Phase 5 (scheduler: heartbeat, deps, resume).

Drives a real file-backed blackboard project through the scheduler with a fake
OpenAI-compatible client, so no API key or Telegram token is needed. Covers: running a
dependency graph to completion, passing upstream outputs as inputs, posting only the
terminal task's result, crash recovery (running -> ready), and resuming after a
restart without redoing completed tasks.
"""

import asyncio
import os

from scratch_fakes import FakeChannel, chat, system_of, text_response, user_of
from starling.agents.roles import ROLE_PROMPTS
from starling.blackboard import Blackboard, TaskStatus
from starling.scheduler import Scheduler

DB = "phase5_scratch.db"
_SYS_TO_ROLE = {prompt: role for role, prompt in ROLE_PROMPTS.items()}


class FakeClient:
    """Returns role-tagged worker output and records the prompt it was given."""

    def __init__(self) -> None:
        self.chat = chat(self._create)
        self.prompts: list[tuple[str, str]] = []  # (role, user_prompt)

    async def _create(self, **kw):
        role = _SYS_TO_ROLE.get(system_of(kw), "?")
        user = user_of(kw)
        self.prompts.append((role, user))
        if role == "researcher":
            return text_response(f"RDATA({user})")
        if role == "summarizer":
            return text_response("FINAL-COMPARISON")
        return text_response("X")

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

    summ_prompt = next(user for role, user in client.prompts if role == "summarizer")
    check("summarizer received upstream researcher outputs", "RDATA(" in summ_prompt)
    check("summarizer output stored", tasks[summarizer]["output"] == "FINAL-COMPARISON")

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

    bb1 = Blackboard(DB)
    pid, researchers, summarizer = build_project(bb1)
    client1 = FakeClient()
    await Scheduler(bb1, FakeChannel(), client1, tick_interval=0.01).tick()
    check("researchers done before crash", all(
        bb1.get_task(r)["status"] == TaskStatus.DONE for r in researchers))
    check("summarizer not yet run", bb1.get_task(summarizer)["status"] == TaskStatus.PENDING)
    check("3 researcher runs in first process", client1.role_calls("researcher") == 3)
    bb1.close()

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

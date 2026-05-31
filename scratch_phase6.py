"""Scratch verification for Phase 6 (human-in-the-loop).

Runs a project containing a decision point end-to-end with a fake OpenAI-compatible
client and a real file-backed blackboard, so no API key or Telegram token is needed.
Covers: the PM emitting a 'pm' question task, the scheduler pausing it (awaiting_human
+ ask), the orchestrator routing the next reply to the waiting task (not classifying
it as a new request), the answer flowing to downstream tasks, and the project finishing.
"""

import asyncio
import os

from scratch_fakes import FakeChannel, chat, system_of, text_response, tool_name, tool_response, user_of
from starling.agents.roles import ROLE_PROMPTS
from starling.blackboard import Blackboard, TaskStatus
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


class FakeClient:
    def __init__(self) -> None:
        self.chat = chat(self._create)
        self.calls: list[str] = []                       # classify | decompose
        self.worker_prompts: list[tuple[str, str]] = []  # (role, user)

    async def _create(self, **kw):
        name = tool_name(kw)
        if name == "classify_request":
            self.calls.append("classify")
            return tool_response("classify_request", CLASSIFICATION)
        if name == "submit_plan":
            self.calls.append("decompose")
            return tool_response("submit_plan", PLAN)
        role = _SYS_TO_ROLE.get(system_of(kw), "?")
        user = user_of(kw)
        self.worker_prompts.append((role, user))
        if role == "researcher":
            return text_response(f"RDATA({user})")
        if role == "summarizer":
            return text_response("ITINERARY")
        return text_response("X")

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
    chat_id = 7

    print("1. user starts a project with a decision point:")
    await orch.handle_message(chat_id, "plan me a weekend trip")
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
          bb.awaiting_human(chat_id) is not None and bb.awaiting_human(chat_id)["id"] == 1)

    print("\n3. user's reply is routed to the waiting task (not a new request):")
    await orch.handle_message(chat_id, "Paris")
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

    print("\nregression: a pending decision blocks project completion:")
    if os.path.exists(DB):
        os.remove(DB)
    bb = Blackboard(DB)
    channel = FakeChannel()
    sched = Scheduler(bb, channel, FakeClient(), tick_interval=0.01)
    pid2 = bb.create_project(7, "g")
    q = bb.add_task("pm", "Which city?", project_id=pid2)            # decision, no deps
    bb.add_task("researcher", "an unrelated sink", project_id=pid2)  # sink that ignores the decision
    await sched.tick()  # pm -> awaiting_human; the researcher runs to done
    check("decision is awaiting_human", bb.get_task(q)["status"] == TaskStatus.AWAITING_HUMAN)
    check("no 'complete' posted while the decision is pending",
          not any("complete" in t.lower() for t in channel.texts()))
    bb.close()

    if os.path.exists(DB):
        os.remove(DB)
    print("\nALL PASS: human-in-the-loop pause, reply routing, and resume verified offline.")


if __name__ == "__main__":
    asyncio.run(main())

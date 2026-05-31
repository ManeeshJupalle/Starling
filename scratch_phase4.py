"""Scratch verification for Phase 4 (project mode: PM decomposition).

Runs decompose + the orchestrator's project flow with a fake OpenAI-compatible client
and a real (file-backed) blackboard, so no API key or Telegram token is needed.
Covers: schema-validated decomposition, graph repair (drop bad deps), rejection of
cycles / too-many-tasks / unknown roles, and the end-to-end project flow that persists
a sane task graph with index deps resolved to blackboard ids — and no execution.
"""

import asyncio
import os

from scratch_fakes import FakeChannel, chat, text_response, tool_name, tool_response
from starling.agents.pm import MAX_TASKS, decompose, topological_order
from starling.blackboard import Blackboard, TaskStatus
from starling.orchestrator import Orchestrator
from starling.schemas import PlannedTask

DB = "phase4_scratch.db"


class FakeClient:
    """Returns the classification for classify_request and the plan for submit_plan."""

    def __init__(self, plan: dict, classification: dict | None = None) -> None:
        self.chat = chat(self._create)
        self._plan = plan
        self._classification = classification
        self.calls: list[str] = []

    async def _create(self, **kw):
        name = tool_name(kw)
        if name == "classify_request":
            self.calls.append("classify")
            return tool_response("classify_request", self._classification)
        if name == "submit_plan":
            self.calls.append("decompose")
            return tool_response("submit_plan", self._plan)
        self.calls.append("text")  # a worker/merge call would mean unwanted execution
        return text_response("UNEXPECTED")


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


async def expect_value_error(coro, label: str) -> None:
    raised = False
    try:
        await coro
    except ValueError:
        raised = True
    check(label, raised)


async def test_decompose_happy() -> None:
    print("decompose: 3 researchers -> 1 summarizer:")
    plan_dict = {"tasks": [
        {"role": "researcher", "description": "research Celery", "depends_on": []},
        {"role": "researcher", "description": "research RQ", "depends_on": []},
        {"role": "researcher", "description": "research Dramatiq", "depends_on": []},
        {"role": "summarizer", "description": "write comparison", "depends_on": [0, 1, 2]},
    ]}
    plan = await decompose("compare task queues", client=FakeClient(plan_dict))
    check("4 tasks", len(plan.tasks) == 4)
    check("roles all valid", all(t.role in ("researcher", "summarizer") for t in plan.tasks))
    check("summarizer depends on the 3 researchers", plan.tasks[3].depends_on == [0, 1, 2])


async def test_decompose_repairs_bad_deps() -> None:
    print("\ndecompose: drops out-of-range and self dependencies:")
    plan_dict = {"tasks": [
        {"role": "researcher", "description": "a", "depends_on": []},
        {"role": "summarizer", "description": "b", "depends_on": [0, 5, 1]},  # 5 oob, 1 self
    ]}
    plan = await decompose("g", client=FakeClient(plan_dict))
    check("kept valid dep [0], dropped 5 (oob) and 1 (self)", plan.tasks[1].depends_on == [0])


async def test_decompose_rejects_cycle() -> None:
    print("\ndecompose: rejects a cyclic graph:")
    plan_dict = {"tasks": [
        {"role": "researcher", "description": "a", "depends_on": [1]},
        {"role": "summarizer", "description": "b", "depends_on": [0]},
    ]}
    await expect_value_error(decompose("g", client=FakeClient(plan_dict)), "cycle raises ValueError")


async def test_decompose_rejects_too_many() -> None:
    print("\ndecompose: rejects more than MAX_TASKS:")
    plan_dict = {"tasks": [
        {"role": "researcher", "description": f"t{i}", "depends_on": []}
        for i in range(MAX_TASKS + 1)
    ]}
    await expect_value_error(
        decompose("g", client=FakeClient(plan_dict)), f"more than {MAX_TASKS} tasks raises"
    )


async def test_decompose_rejects_unknown_role() -> None:
    print("\ndecompose: rejects an unknown role:")
    plan_dict = {"tasks": [{"role": "wizard", "description": "a", "depends_on": []}]}
    await expect_value_error(decompose("g", client=FakeClient(plan_dict)), "unknown role raises")


async def test_gate_on_decision() -> None:
    print("\ndecompose: a single up-front pm decision gates all other tasks:")
    plan_dict = {"tasks": [
        {"role": "pm", "description": "Which city?", "depends_on": []},
        {"role": "researcher", "description": "research", "depends_on": []},   # forgot to gate
        {"role": "summarizer", "description": "write it up", "depends_on": [1]},
    ]}
    plan = await decompose("g", client=FakeClient(plan_dict))
    check("researcher now depends on the pm task", 0 in plan.tasks[1].depends_on)
    check("summarizer is gated too", 0 in plan.tasks[2].depends_on)


def test_topological_order() -> None:
    print("\ntopological_order respects dependencies:")
    tasks = [
        PlannedTask(role="researcher", description="a", depends_on=[]),
        PlannedTask(role="summarizer", description="b", depends_on=[0, 2]),
        PlannedTask(role="researcher", description="c", depends_on=[]),
    ]
    order = topological_order(tasks)
    pos = {idx: p for p, idx in enumerate(order)}
    check("dep 0 before 1", pos[0] < pos[1])
    check("dep 2 before 1", pos[2] < pos[1])


async def test_project_flow_persists_graph() -> None:
    print("\nproject flow: insert graph into blackboard, resolve index deps, no execution:")
    classification = {
        "mode": "project",
        "goal": "research the top 3 Python task-queue libraries and write a short comparison",
        "workers": [],
    }
    plan_dict = {"tasks": [
        {"role": "researcher", "description": "research Celery", "depends_on": []},
        {"role": "researcher", "description": "research RQ", "depends_on": []},
        {"role": "researcher", "description": "research Dramatiq", "depends_on": []},
        {"role": "summarizer", "description": "write the comparison", "depends_on": [0, 1, 2]},
    ]}
    fake = FakeClient(plan_dict, classification)
    channel = FakeChannel()
    bb = Blackboard(DB)
    await Orchestrator(channel, fake, bb).handle_message(7, "compare task queues")

    check("only classify + decompose called (no worker/merge execution)",
          fake.calls == ["classify", "decompose"])
    check("one confirmation reply sent", len(channel.sent) == 1)
    check("reply confirms the project started", "Project #1" in channel.sent[0][1])

    ready = bb.ready_tasks()
    check("3 researcher tasks are ready", [t["id"] for t in ready] == [1, 2, 3])
    check("all ready tasks are researchers", all(t["role"] == "researcher" for t in ready))

    summarizer = bb.get_task(4)
    check("summarizer is pending (blocked on its deps)", summarizer["status"] == TaskStatus.PENDING)
    check("summarizer deps resolved to researcher blackboard ids", summarizer["depends_on"] == [1, 2, 3])
    bb.close()

    bb2 = Blackboard(DB)
    persisted = bb2.get_task(4)
    check("graph persisted across reopen", persisted["status"] == TaskStatus.PENDING
          and persisted["depends_on"] == [1, 2, 3])
    bb2.close()


async def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)
    await test_decompose_happy()
    await test_decompose_repairs_bad_deps()
    await test_decompose_rejects_cycle()
    await test_decompose_rejects_too_many()
    await test_decompose_rejects_unknown_role()
    await test_gate_on_decision()
    test_topological_order()
    await test_project_flow_persists_graph()
    if os.path.exists(DB):
        os.remove(DB)
    print("\nALL PASS: project decomposition + blackboard persistence verified offline.")


if __name__ == "__main__":
    asyncio.run(main())

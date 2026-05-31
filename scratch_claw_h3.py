"""Phase H3 verification: the Morning Brief multi-agent template (offline).

The canned brief is a parallel fan-out (calendar + email + news) feeding a summarizer.
Confirms the template's shape, the routing detection, and that starting it inserts a
real project whose three gatherers are immediately ready while the summarizer waits on
all three. No LLM/tools needed.
"""

import asyncio

from scratch_fakes import FakeChannel
from starling.agents.pm import morning_brief_plan, topological_order
from starling.blackboard import Blackboard, TaskStatus
from starling.orchestrator import Orchestrator, is_morning_brief


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def test_plan_shape() -> None:
    print("morning_brief_plan is a parallel fan-out + merge:")
    plan = morning_brief_plan()
    roles = [t.role for t in plan.tasks]
    check("4 tasks", len(plan.tasks) == 4)
    check("calendar + email + news + summarizer", roles == ["operator", "operator", "researcher", "summarizer"])
    check("the 3 gatherers have no deps (run in parallel)", all(not t.depends_on for t in plan.tasks[:3]))
    check("summarizer merges all three", plan.tasks[3].depends_on == [0, 1, 2])
    check("graph is acyclic", len(topological_order(plan.tasks)) == 4)


def test_detection() -> None:
    print("\nrouting detects the brief:")
    check("'Give me a morning brief'", is_morning_brief("Give me a morning brief"))
    check("'morning briefing please'", is_morning_brief("morning briefing please"))
    check("a normal request is not a brief", not is_morning_brief("what's the weather today"))


async def test_start_inserts_fanout() -> None:
    print("\nstarting the brief inserts a ready fan-out + a waiting summarizer:")
    bb = Blackboard(":memory:")
    orch = Orchestrator(FakeChannel(), None, bb, None)
    msg = await orch.start_morning_brief(5)

    projects = bb.all_projects()
    check("one project created for the chat", len(projects) == 1 and projects[0]["chat_id"] == 5)
    tasks = bb.project_tasks(projects[0]["id"])
    check("four tasks persisted", len(tasks) == 4)
    check("the 3 gatherers are READY now", all(t["status"] == TaskStatus.READY.value for t in tasks[:3]))
    summ = tasks[3]
    check("summarizer is PENDING", summ["status"] == TaskStatus.PENDING.value)
    check("summarizer depends on the 3 gatherers", summ["depends_on"] == [t["id"] for t in tasks[:3]])
    check("user gets a kickoff message", "agents" in msg)


async def main() -> None:
    test_plan_shape()
    test_detection()
    await test_start_inserts_fanout()
    print("\nALL PASS: Morning Brief multi-agent template (Phase H3).")


if __name__ == "__main__":
    asyncio.run(main())

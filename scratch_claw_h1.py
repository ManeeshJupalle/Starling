"""Phase H1 verification: proactive scheduled triggers (offline, deterministic).

Covers the trigger store (create / due-filter / re-arm / disable) and the scheduler
firing due triggers via its on_trigger callback — daily triggers re-arm to a future
time, one-shots retire, and nothing double-fires. A fixed `now` is injected so no real
clock or LLM is involved.
"""

import asyncio
from datetime import datetime

from scratch_fakes import FakeChannel
from starling.blackboard import Blackboard
from starling.scheduler import Scheduler


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def test_trigger_store() -> None:
    print("trigger store: create / due-filter / disable / re-arm:")
    bb = Blackboard(":memory:")
    bb.add_trigger(1, "morning brief", "daily", "2020-01-01T08:00:00")
    bb.add_trigger(1, "future thing", "once", "2999-01-01T08:00:00")
    due = bb.due_triggers("2020-06-01T09:00:00")
    check("only the past-due trigger is due", [t["goal"] for t in due] == ["morning brief"])
    check("both triggers are stored", len(bb.all_triggers()) == 2)
    bb.disable_trigger(due[0]["id"])
    check("disabled trigger drops out of due", bb.due_triggers("2020-06-01T09:00:00") == [])
    tid = bb.add_trigger(2, "later", "daily", "2020-01-01T07:00:00")
    bb.set_trigger_next_run(tid, "2999-01-01T07:00:00")
    check("re-armed trigger no longer due", all(t["goal"] != "later" for t in bb.due_triggers("2020-06-01T09:00:00")))


async def test_fire_and_rearm() -> None:
    print("\nscheduler fires due triggers, re-arms daily, retires once:")
    bb = Blackboard(":memory:")
    bb.add_trigger(7, "daily goal", "daily", "2020-01-01T08:00:00")
    bb.add_trigger(7, "once goal", "once", "2020-01-01T08:00:00")
    bb.add_trigger(7, "future goal", "once", "2999-01-01T08:00:00")
    fired: list[tuple[int, str]] = []

    async def on_trigger(chat_id: int, goal: str) -> None:
        fired.append((chat_id, goal))

    sched = Scheduler(bb, FakeChannel(), None, 5)
    sched.on_trigger = on_trigger
    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 0, 0))

    check("both due goals fired; future did not", sorted(g for _, g in fired) == ["daily goal", "once goal"])
    check("delivered to the trigger's chat", all(cid == 7 for cid, _ in fired))
    trigs = {t["goal"]: t for t in bb.all_triggers()}
    check("daily re-armed (still enabled)", trigs["daily goal"]["enabled"] == 1)
    check("daily advanced to a future firing", trigs["daily goal"]["next_run"] > "2020-01-01T09:00:00")
    check("once retired (disabled)", trigs["once goal"]["enabled"] == 0)
    check("future trigger untouched", trigs["future goal"]["enabled"] == 1)

    fired.clear()
    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 0, 0))
    check("no double-fire on the next pass", fired == [])


async def test_no_callback_is_safe() -> None:
    print("\nno on_trigger wired -> no-op, trigger stays pending:")
    bb = Blackboard(":memory:")
    bb.add_trigger(1, "x", "once", "2020-01-01T00:00:00")
    sched = Scheduler(bb, FakeChannel(), None, 5)
    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 0, 0))
    check("trigger untouched without a callback", bb.due_triggers("2020-01-01T09:00:00") != [])


async def main() -> None:
    test_trigger_store()
    await test_fire_and_rearm()
    await test_no_callback_is_safe()
    print("\nALL PASS: proactive scheduled triggers (Phase H1).")


if __name__ == "__main__":
    asyncio.run(main())

"""Phase H2 verification: inbox-watch event triggers (offline, deterministic).

A watch polls a read tool on an interval and fires its goal only when the result
*changes* (the first poll just records a baseline). Errors are no-ops. Poll results and
the clock are injected, so no Gmail/LLM is touched.
"""

import asyncio
from datetime import datetime

from scratch_fakes import FakeChannel
from starling.blackboard import Blackboard
from starling.scheduler import Scheduler


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def _scheduler(bb, on_trigger, poll):
    sched = Scheduler(bb, FakeChannel(), None, 5)
    sched.on_trigger = on_trigger
    sched.poll_tool = poll
    return sched


def test_add_watch() -> None:
    print("add_watch stores a 'watch' trigger:")
    bb = Blackboard(":memory:")
    bb.add_watch(9, "summarize it", "gmail__search_emails", {"query": "is:unread"}, 300, "2020-01-01T08:00:00")
    t = bb.all_triggers()[0]
    check("kind is 'watch'", t["kind"] == "watch")
    check("appears in due_triggers", bb.due_triggers("2020-06-01T00:00:00")[0]["goal"] == "summarize it")
    check("cursor starts empty", t["cursor"] is None)


async def test_baseline_then_change() -> None:
    print("\nfirst poll baselines; unchanged = no fire; change = fire with context:")
    bb = Blackboard(":memory:")
    bb.add_watch(9, "summarize new mail", "gmail__search_emails", {"query": "is:unread"}, 300, "2020-01-01T08:00:00")
    fired: list[tuple[int, str]] = []
    results = iter(["inbox v1", "inbox v1", "inbox v2"])

    async def on_trigger(chat_id, goal):
        fired.append((chat_id, goal))

    async def poll(name, args):
        return next(results)

    sched = _scheduler(bb, on_trigger, poll)

    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 0, 0))   # baseline
    check("first poll only baselines (no fire)", fired == [])
    check("cursor = baseline snapshot", bb.all_triggers()[0]["cursor"] == "inbox v1")
    check("re-armed into the future", bb.all_triggers()[0]["next_run"] > "2020-01-01T09:00:00")

    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 5, 0))   # unchanged
    check("unchanged inbox does not fire", fired == [])

    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 10, 0))  # changed
    check("change fires exactly once", len(fired) == 1)
    chat_id, goal = fired[0]
    check("delivered to the watch's chat", chat_id == 9)
    check("goal carries the instruction", "summarize new mail" in goal)
    check("goal carries the new content as context", "inbox v2" in goal)
    check("cursor advanced to new snapshot", bb.all_triggers()[0]["cursor"] == "inbox v2")


async def test_error_is_noop() -> None:
    print("\na failing/empty poll never fires and never baselines:")
    bb = Blackboard(":memory:")
    bb.add_watch(1, "g", "gmail__search_emails", {}, 300, "2020-01-01T08:00:00")
    fired: list = []

    async def on_trigger(chat_id, goal):
        fired.append(goal)

    async def poll(name, args):
        return "Error: gmail not connected"

    sched = _scheduler(bb, on_trigger, poll)
    await sched._fire_due_triggers(now=datetime(2020, 1, 1, 9, 0, 0))
    check("error poll does not fire", fired == [])
    check("no baseline recorded on error", bb.all_triggers()[0]["cursor"] is None)
    check("watch still re-armed", bb.all_triggers()[0]["next_run"] > "2020-01-01T09:00:00")


async def main() -> None:
    test_add_watch()
    await test_baseline_then_change()
    await test_error_is_noop()
    print("\nALL PASS: inbox-watch event triggers (Phase H2).")


if __name__ == "__main__":
    asyncio.run(main())

"""Phase E verification: the live web dashboard serves blackboard state + SSE.

Seeds a blackboard with a mid-run project, starts the dashboard, and checks the three
endpoints (state JSON, the HTML page, and the first SSE event). Watching it stream in a
browser is the live demo: run `python -m starling`, open http://127.0.0.1:8000, and
start a project from Telegram.
"""

import asyncio

import aiohttp

from starling.blackboard import Blackboard, TaskStatus
from starling.channels.web import WebDashboard

BASE = "http://127.0.0.1:8123"


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


async def main() -> None:
    bb = Blackboard(":memory:")
    pid = bb.create_project(7, "research the top 3 task queues")
    r1 = bb.add_task("researcher", "research Celery", project_id=pid)
    bb.add_task("researcher", "research RQ", project_id=pid)
    summarizer = bb.add_task("summarizer", "write the comparison", project_id=pid, depends_on=[r1])
    bb.set_status(r1, TaskStatus.DONE, output="Celery is a distributed task queue ...")

    dash = WebDashboard(bb, host="127.0.0.1", port=8123)
    await dash.start()
    try:
        async with aiohttp.ClientSession() as sess:
            print("GET /api/state:")
            async with sess.get(BASE + "/api/state") as resp:
                state = await resp.json()
            project = state["projects"][0]
            check("one project", len(state["projects"]) == 1)
            check("project goal present", project["goal"] == "research the top 3 task queues")
            check("3 tasks", len(project["tasks"]) == 3)
            by_id = {t["id"]: t for t in project["tasks"]}
            check("researcher #1 done with output", by_id[r1]["status"] == "done" and by_id[r1]["output"])
            check("summarizer pending, depends on #" + str(r1),
                  by_id[summarizer]["status"] == "pending" and r1 in by_id[summarizer]["depends_on"])

            print("\nGET / (dashboard page):")
            async with sess.get(BASE + "/") as resp:
                html = await resp.text()
            check("serves an HTML page", "<html" in html.lower() and "Starling" in html)
            check("page wires up the SSE stream", "EventSource('/events')" in html)

            print("\nGET /events (SSE):")
            async with sess.get(BASE + "/events") as resp:
                check("event-stream content type",
                      resp.headers.get("Content-Type", "").startswith("text/event-stream"))
                chunk = await asyncio.wait_for(resp.content.read(2048), timeout=3)
            sse = chunk.decode(errors="replace")
            check("first SSE event carries the live state", "data:" in sse and "task queues" in sse)
    finally:
        await dash.aclose()
    bb.close()
    print("\nALL PASS: live web dashboard serves state + SSE (Phase E).")


if __name__ == "__main__":
    asyncio.run(main())

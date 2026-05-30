"""Scratch verification for Phase 2 (blackboard).

First run builds a project with a dependency and verifies pending -> ready
promotion. Run it again (a new process) against the same .db to confirm the state
persisted to disk.

    python scratch_phase2.py     # fresh run: builds + verifies promotion
    python scratch_phase2.py     # restart: verifies persistence
"""

import os

from starling.blackboard import Blackboard, TaskStatus

DB = "phase2_scratch.db"


def fresh_run(bb: Blackboard) -> None:
    print("FRESH RUN - building project with a dependency\n")
    pid = bb.create_project(chat_id=12345, goal="demo: research then summarize")
    a = bb.add_task("researcher", "gather facts", project_id=pid)            # no deps
    b = bb.add_task("summarizer", "summarize a", project_id=pid, depends_on=[a])

    print(f"  project={pid}  task_a={a}  task_b={b}")
    print(f"  a status = {bb.get_task(a)['status']:14} (expect ready)")
    print(f"  b status = {bb.get_task(b)['status']:14} (expect pending)")
    assert bb.get_task(a)["status"] == TaskStatus.READY
    assert bb.get_task(b)["status"] == TaskStatus.PENDING

    ready_before = [t["id"] for t in bb.ready_tasks()]
    print(f"  ready before dep done: {ready_before}  (b={b} absent)")
    assert b not in ready_before

    print(f"\n  marking task_a ({a}) done...")
    bb.set_status(a, TaskStatus.DONE)

    ready_after = [t["id"] for t in bb.ready_tasks()]
    print(f"  ready after dep done:  {ready_after}  (b={b} present)")
    assert b in ready_after
    assert bb.get_task(b)["status"] == TaskStatus.READY

    print("\nPASS: dependent task promoted pending -> ready once its dep was done.")
    print("Run this script again to verify the state persisted across processes.")


def restart_run(bb: Blackboard) -> None:
    print("RESTART - reading persisted state from", DB, "\n")
    rows = bb._conn.execute(
        "SELECT role, status FROM tasks ORDER BY id"
    ).fetchall()
    by_role = {}
    for r in rows:
        by_role[r["role"]] = r["status"]
        print(f"  {r['role']:11} -> {r['status']}")

    assert by_role["researcher"] == TaskStatus.DONE
    assert by_role["summarizer"] == TaskStatus.READY
    print("\nPASS: state survived a process restart (researcher=done, summarizer=ready).")


def main() -> None:
    is_restart = os.path.exists(DB)
    bb = Blackboard(DB)
    try:
        (restart_run if is_restart else fresh_run)(bb)
    finally:
        bb.close()


if __name__ == "__main__":
    main()

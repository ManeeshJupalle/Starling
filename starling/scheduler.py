"""Scheduler — heartbeat + ready-task dispatch.

Hybrid drive (ARCHITECTURE.md §2.4): a heartbeat wakes every ``tick_interval``
seconds and the orchestrator can ``poke()`` it directly after writing new tasks.
Each pass runs the currently-ready tasks (passing upstream outputs as inputs),
stores their results, and lets newly-unblocked tasks promote on the next pass. When
a terminal task finishes, its result is posted to the project's chat.

Because all state lives in the blackboard, a restart resumes in-flight projects:
``recover()`` requeues tasks interrupted mid-run while leaving completed work alone.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from openai import AsyncOpenAI

from .agents.roles import tools_for_role
from .agents.worker import run_task
from .blackboard import Blackboard, TaskStatus
from .channels.base import Channel


class Scheduler:
    """Drives ready tasks to completion, reading desired state from the blackboard."""

    def __init__(
        self,
        blackboard: Blackboard,
        channel: Channel,
        client: AsyncOpenAI,
        tick_interval: float,
        tools_manager=None,
    ) -> None:
        self._bb = blackboard
        self._channel = channel
        self._client = client
        self._tick_interval = tick_interval
        self._tools_mgr = tools_manager
        self._wake = asyncio.Event()
        self._reported: set[int] = set()  # projects already reported complete

    def poke(self) -> None:
        """Ask the scheduler to run a pass as soon as possible."""
        self._wake.set()

    def recover(self) -> int:
        """Requeue tasks interrupted mid-run so they re-run. Returns the count."""
        return self._bb.reset_running()

    async def run(self) -> None:
        """Heartbeat loop: recover, then tick on each poke or every tick_interval."""
        recovered = self.recover()
        if recovered:
            print(f"[scheduler] requeued {recovered} interrupted task(s) on startup")
        while True:
            await self.tick()
            await self._wait()

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._tick_interval)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def tick(self) -> None:
        """Run all currently-ready tasks; newly-unblocked tasks run on a later pass."""
        ready = self._bb.ready_tasks()
        if not ready:
            return
        await asyncio.gather(*(self._run_one(task) for task in ready))
        self.poke()  # process newly-unblocked tasks without waiting for the heartbeat

    async def _run_one(self, task: dict[str, Any]) -> None:
        if task["role"] == "pm":
            await self._ask_human(task)
            return
        print(f"[scheduler] running task #{task['id']} ({task['role']})")
        self._bb.set_status(task["id"], TaskStatus.RUNNING)
        try:
            inputs = self._gather_inputs(task)
            result = await run_task(
                task["role"], task["description"], inputs, client=self._client,
                tools=tools_for_role(self._tools_mgr, task["role"], allow_sensitive=True),
                allow_sensitive=True,
            )
        except Exception as exc:  # record the failure and move on; dependents stall
            print(f"[scheduler] task #{task['id']} FAILED: {exc}")
            self._bb.set_status(task["id"], TaskStatus.FAILED, output=f"error: {exc}")
            return
        if not result.done:  # the worker wants approval for a sensitive action
            await self._pause_for_approval(task, result)
            return
        self._bb.set_status(task["id"], TaskStatus.DONE, output=result.output)
        task["output"] = result.output  # keep the in-memory dict in sync for reporting
        print(f"[scheduler] task #{task['id']} done")
        await self.report_if_complete(task["project_id"])

    async def _pause_for_approval(self, task: dict[str, Any], result) -> None:
        """Park a task that wants a sensitive action and ask the user to approve it."""
        checkpoint = {"messages": result.messages, "remaining": result.remaining}
        self._bb.set_status(
            task["id"], TaskStatus.AWAITING_HUMAN, question=result.question, checkpoint=checkpoint
        )
        print(f"[scheduler] task #{task['id']} awaiting approval - asked in chat")
        project = self._bb.get_project(task["project_id"]) if task["project_id"] else None
        if project is not None:
            await self._channel.send(project["chat_id"], result.question)

    async def _ask_human(self, task: dict[str, Any]) -> None:
        """Pause a 'pm' question task: store the question and ask it in the chat.

        The task stays ``awaiting_human`` until the orchestrator routes the user's next
        reply back to it (ARCHITECTURE.md §3).
        """
        self._bb.set_status(
            task["id"], TaskStatus.AWAITING_HUMAN, question=task["description"]
        )
        print(f"[scheduler] task #{task['id']} awaiting human - asked in chat")
        project = self._bb.get_project(task["project_id"]) if task["project_id"] else None
        if project is not None:
            await self._channel.send(project["chat_id"], task["description"])

    def _gather_inputs(self, task: dict[str, Any]) -> dict[str, Any]:
        """Collect upstream task outputs to feed this task (ARCHITECTURE.md §2.5)."""
        if not task["depends_on"]:
            return {}
        upstream = []
        for dep_id in task["depends_on"]:
            dep = self._bb.get_task(dep_id)
            if dep is not None:
                upstream.append(
                    {"role": dep["role"], "description": dep["description"], "output": dep["output"]}
                )
        return {"upstream_results": upstream}

    async def report_if_complete(self, project_id: Optional[int]) -> None:
        """Post the project's result once every task is finished — and not before.

        Gating on the whole project being settled (no task still pending, ready,
        running, or awaiting_human) means an unanswered decision blocks completion,
        rather than a stray terminal task declaring the project done prematurely.
        Called whenever a task settles — a worker finishing or a human answering.
        """
        if project_id is None or project_id in self._reported:
            return
        tasks = self._bb.project_tasks(project_id)
        active = {
            TaskStatus.PENDING.value,
            TaskStatus.READY.value,
            TaskStatus.RUNNING.value,
            TaskStatus.AWAITING_HUMAN.value,
        }
        if any(t["status"] in active for t in tasks):
            return  # still working, or waiting on a human answer
        project = self._bb.get_project(project_id)
        if project is None:
            return
        # Post the output of the terminal task(s) — the ones nothing depends on.
        sinks = [
            t for t in tasks
            if t["status"] == TaskStatus.DONE
            and not any(t["id"] in other["depends_on"] for other in tasks)
        ]
        result = "\n\n".join(t["output"] for t in sinks if t["output"]) or "(no output)"
        self._reported.add(project_id)
        print(f"[scheduler] project #{project_id} complete")
        await self._channel.send(project["chat_id"], f"Project #{project_id} complete:\n\n{result}")

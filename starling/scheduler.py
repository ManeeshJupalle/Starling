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
from typing import Any

from anthropic import AsyncAnthropic

from .agents.worker import run_task
from .blackboard import Blackboard, TaskStatus
from .channels.base import Channel


class Scheduler:
    """Drives ready tasks to completion, reading desired state from the blackboard."""

    def __init__(
        self,
        blackboard: Blackboard,
        channel: Channel,
        client: AsyncAnthropic,
        tick_interval: float,
    ) -> None:
        self._bb = blackboard
        self._channel = channel
        self._client = client
        self._tick_interval = tick_interval
        self._wake = asyncio.Event()

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
        self._bb.set_status(task["id"], TaskStatus.RUNNING)
        try:
            inputs = self._gather_inputs(task)
            output = await run_task(
                task["role"], task["description"], inputs, client=self._client
            )
        except Exception as exc:  # record the failure and move on; dependents stall
            self._bb.set_status(task["id"], TaskStatus.FAILED, output=f"error: {exc}")
            return
        self._bb.set_status(task["id"], TaskStatus.DONE, output=output)
        task["output"] = output  # keep the in-memory dict in sync for reporting
        await self._report_if_terminal(task)

    async def _ask_human(self, task: dict[str, Any]) -> None:
        """Pause a 'pm' question task: store the question and ask it in the chat.

        The task stays ``awaiting_human`` until the orchestrator routes the user's next
        reply back to it (ARCHITECTURE.md §3).
        """
        self._bb.set_status(
            task["id"], TaskStatus.AWAITING_HUMAN, question=task["description"]
        )
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

    async def _report_if_terminal(self, task: dict[str, Any]) -> None:
        """Post the result of a terminal task (nothing depends on it) to its chat."""
        project_id = task["project_id"]
        if project_id is None:
            return
        tasks = self._bb.project_tasks(project_id)
        if any(task["id"] in other["depends_on"] for other in tasks):
            return  # something downstream still depends on this task
        project = self._bb.get_project(project_id)
        if project is None:
            return
        await self._channel.send(
            project["chat_id"], f"Project #{project_id} complete:\n\n{task['output']}"
        )

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
import json
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI

from .agents.critic import critique
from .agents.roles import tools_for_role
from .agents.worker import run_task
from .blackboard import Blackboard, TaskStatus
from . import usage
from .channels.base import Channel
from .memory import recall_context


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
        # Set by wiring (__main__) to the orchestrator's run_goal; lets a due trigger
        # start a project/answer. Kept as a callback to avoid a scheduler<->orchestrator
        # import cycle.
        self.on_trigger: Optional[Callable[[int, str], Awaitable[None]]] = None
        # Set by wiring to a (tool_name, args) -> result-text caller; lets a watch trigger
        # poll an MCP read tool (e.g. gmail__search_emails). Injectable for tests.
        self.poll_tool: Optional[Callable[[str, dict], Awaitable[str]]] = None

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
        """Fire any due triggers, then run all currently-ready tasks."""
        await self._fire_due_triggers()
        ready = self._bb.ready_tasks()
        if not ready:
            return
        await asyncio.gather(*(self._run_one(task) for task in ready))
        self.poke()  # process newly-unblocked tasks without waiting for the heartbeat

    async def _fire_due_triggers(self, now: Optional[datetime] = None) -> None:
        """Run the goal of every trigger whose time has come, then re-arm or retire it.

        The trigger is advanced/disabled *before* its goal runs, so a slow run can't let
        the same trigger fire twice on the next heartbeat. ``now`` is injectable for tests.
        """
        if self.on_trigger is None:
            return
        now = now or datetime.now().replace(microsecond=0)
        for trig in self._bb.due_triggers(now.isoformat()):
            if trig.get("kind") == "watch":
                await self._fire_watch(trig, now)
                continue
            self._advance_trigger(trig, now)
            print(f"[scheduler] firing trigger #{trig['id']} -> {trig['goal'][:50]}")
            try:
                await self.on_trigger(trig["chat_id"], trig["goal"])
            except Exception as exc:
                print(f"[scheduler] trigger #{trig['id']} failed: {exc}")

    async def _fire_watch(self, trig: dict[str, Any], now: datetime) -> None:
        """Poll a watch's tool; if its result changed since last time, fire the goal.

        The first successful poll only records a baseline (so an existing inbox isn't
        treated as 'new'); later changes fire. Re-armed every poll regardless of outcome.
        """
        spec = json.loads(trig["watch"])
        interval_s = int(spec.get("interval_s", 300))
        self._bb.set_trigger_next_run(trig["id"], (now + timedelta(seconds=interval_s)).isoformat())
        if self.poll_tool is None:
            return
        try:
            result = (await self.poll_tool(spec["tool"], spec.get("query") or {})) or ""
        except Exception as exc:
            print(f"[scheduler] watch #{trig['id']} poll failed: {exc}")
            return
        if not result or result.lower().startswith("error"):
            return  # nothing to compare against; try again next interval
        if trig["cursor"] is None:  # first poll: establish the baseline, don't fire
            self._bb.set_trigger_cursor(trig["id"], result)
            return
        if result != trig["cursor"]:
            self._bb.set_trigger_cursor(trig["id"], result)
            print(f"[scheduler] watch #{trig['id']} detected a change -> firing")
            if self.on_trigger is not None:
                await self.on_trigger(
                    trig["chat_id"], f"{trig['goal']}\n\n--- New inbox content ---\n{result}"
                )

    def _advance_trigger(self, trig: dict[str, Any], now: datetime) -> None:
        """Re-arm a daily trigger for its next future firing; retire a one-shot."""
        if trig["recurrence"] == "daily":
            nxt = datetime.fromisoformat(trig["next_run"])
            while nxt <= now:  # skip any missed days in one step (no burst of catch-up fires)
                nxt += timedelta(days=1)
            self._bb.set_trigger_next_run(trig["id"], nxt.isoformat())
        else:
            self._bb.disable_trigger(trig["id"])

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
                allow_sensitive=True, memory=self._recall_memory(task),
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

    def _recall_memory(self, task: dict[str, Any]) -> str:
        """What's known about the task's user, for injecting into the worker prompt."""
        if not task["project_id"]:
            return ""
        project = self._bb.get_project(task["project_id"])
        return recall_context(self._bb, project["chat_id"]) if project else ""

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
        result = await self._review(project["goal"], result)  # critic pass before delivery
        self._reported.add(project_id)
        print(f"[scheduler] project #{project_id} complete  |  usage: {usage.snapshot()}")
        await self._channel.send(project["chat_id"], f"Project #{project_id} complete:\n\n{result}")

    async def _review(self, goal: str, draft: str) -> str:
        """Run the critic over a deliverable; return the approved or corrected text.

        Best-effort: if the critic is unavailable or errors, the draft ships as-is — a
        verify step must never block delivery. An unfixable concern is appended as a note.
        """
        if self._client is None or not draft or draft == "(no output)":
            return draft
        try:
            verdict = await critique(goal, draft, client=self._client)
        except Exception as exc:
            print(f"[scheduler] critic failed (delivering as-is): {exc}")
            return draft
        if verdict.ok:
            print("[scheduler] critic: approved")
            return draft
        if verdict.revised:
            print(f"[scheduler] critic revised the deliverable: {verdict.reason[:60]}")
            return verdict.revised
        print(f"[scheduler] critic flagged (unfixable): {verdict.reason[:60]}")
        return f"{draft}\n\n(Note from review: {verdict.reason})"

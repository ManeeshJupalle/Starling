"""Orchestrator — the brain.

On each inbound message it makes one structured, Pydantic-validated model call to
classify the request (ephemeral vs project), then routes it. Phase 3 implements
ephemeral mode end-to-end: fan out to the chosen workers in parallel, merge their
drafts into one reply, and send it. Project mode (Phase 4) and human-reply routing
(Phase 6) build on this. See ARCHITECTURE.md §2.2.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from anthropic import AsyncAnthropic

from .agents.pm import decompose, topological_order
from .agents.roles import DEFAULT_MODEL, WORKER_ROLES
from .agents.worker import run_task
from .blackboard import Blackboard, TaskStatus
from .channels.base import Channel
from .scheduler import Scheduler
from .schemas import Classification, Mode

_CLASSIFIER_SYSTEM = (
    "You route a user's request inside a multi-agent assistant.\n"
    "- mode 'ephemeral': a one-shot question or task answerable now by fanning out to "
    "workers and merging their replies.\n"
    "- mode 'project': a multi-step, longer-running goal that needs a plan of "
    "dependent tasks.\n"
    "'goal' restates the request as one clear instruction.\n"
    f"'workers' lists the roles to handle an ephemeral request, chosen from: "
    f"{', '.join(WORKER_ROLES)} (pick only the few that genuinely apply)."
)

_MERGE_SYSTEM = (
    "You merge several worker drafts into a single coherent reply for the user. "
    "Resolve overlaps, keep it well-structured and concise, and do not mention that "
    "multiple workers were involved."
)


class Orchestrator:
    """Classifies and routes inbound messages, sending replies via the channel."""

    def __init__(
        self,
        channel: Channel,
        client: AsyncAnthropic,
        blackboard: Blackboard,
        scheduler: Optional[Scheduler] = None,
    ) -> None:
        self._channel = channel
        self._client = client
        self._bb = blackboard
        self._scheduler = scheduler

    async def handle_message(self, chat_id: int, text: str) -> None:
        """Route a reply to a task awaiting a human decision; else classify as new."""
        try:
            paused = self._bb.awaiting_human(chat_id)
            if paused is not None:
                await self._answer_human(chat_id, paused, text)
                return
            classification = await self._classify(text)
            if classification.mode == Mode.EPHEMERAL:
                reply = await self._run_ephemeral(classification)
            else:
                reply = await self._start_project(chat_id, classification)
            await self._channel.send(chat_id, reply)
        except Exception as exc:  # keep the bot responsive on any failure
            await self._channel.send(chat_id, f"Sorry — I hit an error: {exc}")

    async def _answer_human(self, chat_id: int, task: dict[str, Any], text: str) -> None:
        """Store the user's reply as the paused task's answer and resume the project."""
        self._bb.set_status(task["id"], TaskStatus.DONE, output=text)
        if self._scheduler is not None:
            self._scheduler.poke()  # newly-unblocked tasks can run now
        await self._channel.send(chat_id, "Got it - continuing.")

    async def _classify(self, text: str) -> Classification:
        """One structured call; the result is validated before it is acted on."""
        tool = {
            "name": "classify_request",
            "description": "Classify the request and choose the workers to handle it.",
            "input_schema": Classification.model_json_schema(),
        }
        resp = await self._client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=512,
            system=_CLASSIFIER_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "classify_request"},
            messages=[{"role": "user", "content": text}],
        )
        return Classification.model_validate(_tool_use_input(resp))

    async def _run_ephemeral(self, classification: Classification) -> str:
        """Fan out to the chosen workers in parallel, then merge into one reply."""
        workers = [w for w in classification.workers if w in WORKER_ROLES] or ["summarizer"]
        outputs = await asyncio.gather(
            *(run_task(role, classification.goal, client=self._client) for role in workers)
        )
        drafts = list(zip(workers, outputs))
        if len(drafts) == 1:
            return drafts[0][1]
        return await self._merge(classification.goal, drafts)

    async def _start_project(self, chat_id: int, classification: Classification) -> str:
        """Decompose the goal and persist the task graph to the blackboard.

        Tasks are inserted in topological order so each task's index dependencies are
        already resolved to blackboard ids by the time it is added. No execution yet —
        the scheduler (a later phase) drives the tasks to completion.
        """
        plan = await decompose(classification.goal, client=self._client)
        project_id = self._bb.create_project(chat_id, classification.goal)
        id_by_index: dict[int, int] = {}
        for index in topological_order(plan.tasks):
            task = plan.tasks[index]
            dep_ids = [id_by_index[dep] for dep in task.depends_on]
            id_by_index[index] = self._bb.add_task(
                task.role, task.description, project_id=project_id, depends_on=dep_ids
            )
        if self._scheduler is not None:
            self._scheduler.poke()  # start executing the new tasks now
        return f"Project #{project_id} started - {len(plan.tasks)} tasks queued."

    async def _merge(self, goal: str, drafts: list[tuple[str, str]]) -> str:
        joined = "\n\n".join(f"[{role}]\n{output}" for role, output in drafts)
        resp = await self._client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1024,
            system=_MERGE_SYSTEM,
            messages=[
                {"role": "user", "content": f"User request:\n{goal}\n\nWorker drafts:\n{joined}"}
            ],
        )
        return _text(resp)


def _tool_use_input(resp: Any) -> dict[str, Any]:
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("classifier did not return a tool_use block")


def _text(resp: Any) -> str:
    return "".join(block.text for block in resp.content if block.type == "text").strip()

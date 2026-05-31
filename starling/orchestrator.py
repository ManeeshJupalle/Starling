"""Orchestrator — the brain.

For each inbound message it first checks the blackboard for a task awaiting a human
reply in this chat and routes the message there if one exists; otherwise it makes one
structured, Pydantic-validated model call to classify the request and routes it.
Ephemeral mode fans out to workers in parallel and merges; project mode decomposes
the goal and persists the task graph. See ARCHITECTURE.md §2.2.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from openai import AsyncOpenAI

from .agents.pm import decompose, morning_brief_plan, topological_order
from .agents.roles import WORKER_ROLES, active_model, tools_for_role
from .agents.worker import resume_task, run_task
from .blackboard import Blackboard, TaskStatus
from .channels.base import Channel
from . import usage
from .llm import text_of, tool_args
from .memory import recall_context
from .scheduler import Scheduler
from .schemas import Classification, Mode, ScheduleSpec, WatchSpec

_CLASSIFIER_SYSTEM = (
    "You route a user's request inside a multi-agent assistant. Choose the mode and "
    "restate the user's overall objective as 'goal'.\n"
    "\n"
    "Choose mode 'project' when the request is multi-step or needs coordination — it "
    "requires gathering/researching from several angles and then synthesizing, OR it "
    "asks you to make a decision or to ask the user something before producing the "
    "result, OR it otherwise can't be answered well in a single reply.\n"
    "Choose mode 'ephemeral' only when it is a single question or one piece of content "
    "you can answer in one reply by fanning out to a few workers and merging.\n"
    "\n"
    "Examples:\n"
    "- 'summarize the pros and cons of SQLite vs Postgres' -> ephemeral\n"
    "- 'suggest a good name for a cat' -> ephemeral\n"
    "- 'research the top 3 Python task-queue libraries and write a comparison' -> project\n"
    "- 'plan a weekend itinerary, but ask me which city first' -> project\n"
    "- 'help me put together a launch plan for my app' -> project\n"
    "\n"
    "'goal' is the user's overall objective as one clear instruction (e.g. 'Plan a "
    "weekend itinerary'), NOT a sub-step like 'ask the user which city'.\n"
    f"'workers' applies only to ephemeral requests: the roles to fan out to, chosen from "
    f"{', '.join(WORKER_ROLES)} (pick only the few that apply; leave empty for projects). "
    "Use 'operator' for anything touching the user's own email, calendar, or Slack — "
    "reading them (e.g. 'what's on my calendar?') or acting on them.\n"
    "'memory': if this message states a DURABLE preference or fact about the user worth "
    "remembering for future requests (e.g. 'I prefer concise answers', 'I use Python', "
    "'I'm vegetarian'), put a short third-person statement there; otherwise null. Do not "
    "put one-off task details in memory.\n"
    "'schedule': set ONLY when the user asks to set up a recurring or future task — "
    "'every morning brief me', 'remind me daily at 8am to stretch', 'at 18:30 summarize "
    "my day'. Fill {recurrence: 'daily'|'once', at: 'HH:MM' 24-hour}, and put the task "
    "itself in 'goal' (e.g. goal='Give me a morning brief'). A request to do something "
    "right now is NOT a schedule — leave it null.\n"
    "'watch': set ONLY when the user asks to watch their inbox / react to incoming email "
    "— 'when an email from my boss arrives, draft a reply', 'watch for new unread mail "
    "and summarize it'. Fill {query: a Gmail search like 'is:unread' or 'from:boss@x.com', "
    "every_minutes: how often to check}, and put what to do in 'goal'. Otherwise null."
)

_MERGE_SYSTEM = (
    "You merge several worker drafts into a single coherent reply for the user. "
    "Resolve overlaps, keep it well-structured and concise, and do not mention that "
    "multiple workers were involved."
)

_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_request",
        "description": "Classify the request and choose the workers to handle it.",
        "parameters": Classification.model_json_schema(),
    },
}


_AFFIRMATIVE = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "approve", "approved", "go", "do"}


def _is_affirmative(text: str) -> bool:
    """Treat only a clear yes as approval; anything else is a safe-default no."""
    words = text.strip().lower().split()
    return bool(words) and words[0] in _AFFIRMATIVE


def is_morning_brief(text: str) -> bool:
    """Whether a goal is the canned morning-brief template (Phase H3)."""
    t = text.lower()
    return "morning brief" in t or "daily brief" in t or "morning briefing" in t


class Orchestrator:
    """Classifies and routes inbound messages, sending replies via the channel."""

    def __init__(
        self,
        channel: Channel,
        client: AsyncOpenAI,
        blackboard: Blackboard,
        scheduler: Optional[Scheduler] = None,
        tools_manager=None,
    ) -> None:
        self._channel = channel
        self._client = client
        self._bb = blackboard
        self._scheduler = scheduler
        self._tools_mgr = tools_manager

    async def handle_message(self, chat_id: int, text: str) -> None:
        """Route a reply to a task awaiting a human decision; else classify as new."""
        try:
            paused = self._bb.awaiting_human(chat_id)
            if paused is not None:
                print(f"[orchestrator] routing reply to paused task #{paused['id']}")
                await self._answer_human(chat_id, paused, text)
                return
            classification = await self._classify(text)
            print(f"[orchestrator] classified as {classification.mode.value}: {classification.goal[:70]}")
            if classification.memory:
                self._bb.add_memory(chat_id, classification.memory)
                print(f"[orchestrator] remembered: {classification.memory[:60]}")
            if classification.schedule is not None:
                await self._channel.send(
                    chat_id, self._create_trigger(chat_id, classification.goal, classification.schedule)
                )
                return
            if classification.watch is not None:
                await self._channel.send(
                    chat_id, self._create_watch(chat_id, classification.goal, classification.watch)
                )
                return
            if is_morning_brief(classification.goal):
                await self._channel.send(chat_id, await self.start_morning_brief(chat_id))
                return
            if classification.mode == Mode.EPHEMERAL:
                reply = await self._run_ephemeral(chat_id, classification)
            else:
                reply = await self._start_project(chat_id, classification)
            await self._channel.send(chat_id, reply)
        except Exception as exc:  # keep the bot responsive on any failure
            print(f"[orchestrator] error: {exc}")
            await self._channel.send(chat_id, f"Sorry — I hit an error: {exc}")

    async def _answer_human(self, chat_id: int, task: dict[str, Any], text: str) -> None:
        """Route the reply: a tool-approval (task has a checkpoint) or a PM-question answer."""
        if task.get("checkpoint"):
            await self._resume_tool_approval(chat_id, task, text)
            return
        # PM-question answer: store it as the task's output and resume the project.
        self._bb.set_status(task["id"], TaskStatus.DONE, output=text)
        await self._channel.send(chat_id, "Got it - continuing.")
        if self._scheduler is not None:
            self._scheduler.poke()  # newly-unblocked tasks can run now
            # If the answer finished the last task, report now — no worker will fire.
            await self._scheduler.report_if_complete(task["project_id"])

    async def _resume_tool_approval(self, chat_id: int, task: dict[str, Any], text: str) -> None:
        """Resume a worker paused on a sensitive tool call, per the user's yes/no."""
        approved = _is_affirmative(text)
        checkpoint = task["checkpoint"]
        tools = tools_for_role(self._tools_mgr, task["role"], allow_sensitive=True)
        self._bb.set_status(task["id"], TaskStatus.RUNNING)
        await self._channel.send(chat_id, "Approved - running it." if approved else "Okay, skipping that.")
        try:
            result = await resume_task(
                task["role"], checkpoint["messages"], checkpoint["remaining"], approved,
                client=self._client, tools=tools,
            )
        except Exception as exc:
            print(f"[orchestrator] resume error: {exc}")
            self._bb.set_status(task["id"], TaskStatus.FAILED, output=f"error: {exc}")
            return
        if not result.done:  # the worker wants approval for another sensitive action
            self._bb.set_status(
                task["id"], TaskStatus.AWAITING_HUMAN, question=result.question,
                checkpoint={"messages": result.messages, "remaining": result.remaining},
            )
            await self._channel.send(chat_id, result.question)
            return
        self._bb.set_status(task["id"], TaskStatus.DONE, output=result.output)
        if self._scheduler is not None:
            self._scheduler.poke()
            await self._scheduler.report_if_complete(task["project_id"])

    async def _classify(self, text: str) -> Classification:
        """One structured call; the result is validated before it is acted on."""
        resp = await self._client.chat.completions.create(
            model=active_model(),
            max_tokens=512,
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {"role": "user", "content": text},
            ],
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "function", "function": {"name": "classify_request"}},
        )
        usage.record(resp)
        return Classification.model_validate(tool_args(resp))

    async def _run_ephemeral(self, chat_id: int, classification: Classification) -> str:
        """Fan out to the chosen workers in parallel, then merge into one reply."""
        memory = recall_context(self._bb, chat_id)
        workers = [w for w in classification.workers if w in WORKER_ROLES] or ["summarizer"]
        results = await asyncio.gather(
            *(
                run_task(role, classification.goal, client=self._client,
                         tools=tools_for_role(self._tools_mgr, role, allow_sensitive=False),
                         memory=memory)
                for role in workers
            )
        )
        drafts = [(role, result.output) for role, result in zip(workers, results)]
        if len(drafts) == 1:
            return drafts[0][1]
        return await self._merge(classification.goal, drafts, memory)

    async def _start_project(self, chat_id: int, classification: Classification) -> str:
        """Decompose the goal and persist the task graph to the blackboard.

        Tasks are inserted in topological order so each task's index dependencies are
        already resolved to blackboard ids by the time it is added. No execution yet —
        the scheduler drives the tasks to completion.
        """
        plan = await decompose(classification.goal, client=self._client)
        project_id = self._insert_plan(chat_id, classification.goal, plan)
        return f"Project #{project_id} started - {len(plan.tasks)} tasks queued."

    def _insert_plan(self, chat_id: int, goal: str, plan) -> int:
        """Persist a ProjectPlan's tasks in dependency order; return the project id."""
        project_id = self._bb.create_project(chat_id, goal)
        id_by_index: dict[int, int] = {}
        for index in topological_order(plan.tasks):
            task = plan.tasks[index]
            dep_ids = [id_by_index[dep] for dep in task.depends_on]
            id_by_index[index] = self._bb.add_task(
                task.role, task.description, project_id=project_id, depends_on=dep_ids
            )
        print(f"[orchestrator] project #{project_id}: {len(plan.tasks)} tasks queued")
        if self._scheduler is not None:
            self._scheduler.poke()  # start executing the new tasks now
        return project_id

    async def start_morning_brief(self, chat_id: int) -> str:
        """Kick off the canned morning-brief project (parallel fan-out + merge, H3)."""
        plan = morning_brief_plan()
        self._insert_plan(chat_id, "Morning brief", plan)
        return f"Good morning! Putting your brief together - {len(plan.tasks)} agents on it. Results shortly."

    async def run_goal(self, chat_id: int, goal: str) -> None:
        """Classify and run a goal proactively (a trigger fired it), delivering to the chat.

        Like ``handle_message`` but without the awaiting-human check or schedule handling:
        a firing trigger *executes* its goal, it does not re-schedule it. Ephemeral
        results are sent here; project results arrive later via ``report_if_complete``.
        """
        try:
            if is_morning_brief(goal):
                await self._channel.send(chat_id, await self.start_morning_brief(chat_id))
                return
            classification = await self._classify(goal)
            print(f"[orchestrator] trigger fired -> {classification.mode.value}: {goal[:60]}")
            if classification.mode == Mode.EPHEMERAL:
                reply = await self._run_ephemeral(chat_id, classification)
                await self._channel.send(chat_id, reply)
            else:
                await self._start_project(chat_id, classification)
        except Exception as exc:
            print(f"[orchestrator] trigger run error: {exc}")
            await self._channel.send(chat_id, f"Sorry - a scheduled task hit an error: {exc}")

    def _create_trigger(self, chat_id: int, goal: str, spec: ScheduleSpec) -> str:
        """Persist a proactive trigger for ``spec`` and return a human confirmation."""
        now = datetime.now().replace(second=0, microsecond=0)
        try:
            hh, mm = (int(p) for p in spec.at.strip().split(":"))
            target = now.replace(hour=hh, minute=mm)
        except (ValueError, TypeError):
            target = now + timedelta(minutes=1)  # unparseable time -> fire shortly
        if target <= now:
            target += timedelta(days=1)  # time already passed today -> next occurrence
        self._bb.add_trigger(chat_id, goal, spec.recurrence, target.isoformat())
        clock = target.strftime("%H:%M")
        when = f"every day at {clock}" if spec.recurrence == "daily" else f"at {clock} on {target:%b %d}"
        print(f"[orchestrator] scheduled '{goal[:40]}' {when}")
        return f"Scheduled - I'll handle \"{goal}\" {when}."

    def _create_watch(self, chat_id: int, goal: str, spec: WatchSpec) -> str:
        """Persist an inbox watch (polls Gmail search) and return a human confirmation."""
        interval_s = max(60, int(spec.every_minutes) * 60)
        now = datetime.now().replace(microsecond=0)  # next_run = now -> baseline on next tick
        self._bb.add_watch(
            chat_id, goal, "gmail__search_emails", {"query": spec.query}, interval_s, now.isoformat()
        )
        mins = interval_s // 60
        print(f"[orchestrator] watching inbox '{spec.query}' every {mins}m -> {goal[:40]}")
        return (f"Watching your inbox for \"{spec.query}\" every {mins} min - "
                f"I'll \"{goal}\" when something new arrives.")

    async def _merge(self, goal: str, drafts: list[tuple[str, str]], memory: str = "") -> str:
        joined = "\n\n".join(f"[{role}]\n{output}" for role, output in drafts)
        system = _MERGE_SYSTEM + (f"\n\nUser context to honor:\n{memory}" if memory else "")
        resp = await self._client.chat.completions.create(
            model=active_model(),
            max_tokens=1024,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"User request:\n{goal}\n\nWorker drafts:\n{joined}"},
            ],
        )
        usage.record(resp)
        return text_of(resp)

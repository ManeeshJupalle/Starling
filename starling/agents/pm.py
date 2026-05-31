"""PM planner: decompose(goal) -> ProjectPlan.

One structured call using the pm role prompt, turning a goal into a small, acyclic
task graph. Constraints are enforced hard (ARCHITECTURE.md §4): roles restricted to
the worker set (plus 'pm' for a decision point), at most ``MAX_TASKS`` tasks, and
``depends_on`` given as indices into the returned list. Out-of-range and self
dependencies are *repaired* (dropped); unknown roles, too many tasks, and cycles are
*rejected* (raise ValueError).
"""

from __future__ import annotations

from typing import Optional

from openai import AsyncOpenAI

from .. import usage
from ..llm import make_client, tool_args
from ..schemas import PlannedTask, ProjectPlan
from .roles import PLAN_ROLES, ROLE_PROMPTS, pm_model

MAX_TASKS = 12

_DECOMPOSE_GUIDANCE = (
    "\n\nDecompose the user's goal into a minimal set of concrete tasks (usually 2-5, "
    "at most {max}).\n"
    "- Each task's 'role' must be one of: {roles}.\n"
    "- 'depends_on' lists the 0-based indices of other tasks in your list that must "
    "finish before this one. Leave it empty for tasks that can start immediately.\n"
    "- A task receives ONLY the outputs of the tasks listed in its 'depends_on'. So if a "
    "task uses another task's result — saving it, choosing among it, summarizing it, or "
    "building on it — it MUST list that task in 'depends_on', or it will run blind without "
    "the data it needs. Concretely: a task that SAVES or WRITES a chosen item must depend "
    "on the task that PRODUCED that item (and on any task that selected among the options).\n"
    "- Prefer independent gathering/research tasks that can run in parallel, feeding a "
    "final task that produces the user-facing deliverable.\n"
    "- If the goal needs a decision from the user, make the FIRST task a 'pm' task whose "
    "description is the question phrased directly TO the user in the first person (e.g. "
    "\"Which city would you like to visit?\"), never an instruction like \"Ask the user "
    "...\". EVERY task that needs the answer — including the final deliverable — must list "
    "that 'pm' task in its depends_on, so nothing runs until the user has answered.\n"
    "- The dependency graph must be acyclic."
).format(max=MAX_TASKS, roles=", ".join(PLAN_ROLES))

_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "Submit the decomposed project plan as a list of tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "maxItems": MAX_TASKS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": list(PLAN_ROLES)},
                            "description": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "0-based indices of tasks in this list that must finish first",
                            },
                        },
                        "required": ["role", "description"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
}

# Lazily-created shared client so importing this module needs no API key (e.g. tests).
_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = make_client()  # reads LLM_API_KEY / LLM_BASE_URL from the environment
    return _client


def morning_brief_plan() -> ProjectPlan:
    """A canned multi-agent 'morning brief' (Phase H3).

    Three specialists gather in parallel — calendar, inbox, news — and a summarizer
    merges them into one digest. Hand-built rather than PM-decomposed so the flagship
    proactive demo is deterministic; it's a normal project once inserted, so the
    scheduler runs the fan-out concurrently and delivers the result like any other.
    """
    return ProjectPlan(tasks=[
        PlannedTask(role="operator", depends_on=[],
                    description="List everything on my calendar for today, with times. "
                                "If you can't access the calendar, say so briefly."),
        PlannedTask(role="operator", depends_on=[],
                    description="Summarize my unread emails from today: sender and a "
                                "one-line gist each. If you can't access email, say so briefly."),
        PlannedTask(role="researcher", depends_on=[],
                    description="Give 3-5 top world and technology news headlines for today, "
                                "one line each."),
        PlannedTask(role="summarizer", depends_on=[0, 1, 2],
                    description="Write a concise 'good morning' brief that combines today's "
                                "calendar, the email summary, and the news headlines into a "
                                "single friendly digest with short sections."),
    ])


def topological_order(tasks: list[PlannedTask]) -> list[int]:
    """Return task indices in dependency order. Raises ValueError on a cycle."""
    n = len(tasks)
    indegree = [0] * n
    dependents: list[list[int]] = [[] for _ in range(n)]
    for i, task in enumerate(tasks):
        for dep in task.depends_on:
            dependents[dep].append(i)
            indegree[i] += 1
    queue = [i for i in range(n) if indegree[i] == 0]
    order: list[int] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(order) != n:
        raise ValueError("plan dependency graph has a cycle")
    return order


def _gate_on_decision(plan: ProjectPlan) -> ProjectPlan:
    """If the plan has a single up-front 'pm' decision, make every task wait for it.

    A weak planner sometimes forgets to wire the dependency, letting work run before
    the user has answered. When there's exactly one 'pm' task with no dependencies (an
    "ask me first" decision), enforce that everything else depends on it.
    """
    pm_indices = [i for i, t in enumerate(plan.tasks) if t.role == "pm"]
    if len(pm_indices) != 1:
        return plan
    gate = pm_indices[0]
    if plan.tasks[gate].depends_on:
        return plan  # the decision itself has prerequisites; don't risk a cycle
    gated = []
    for i, task in enumerate(plan.tasks):
        if i == gate or gate in task.depends_on:
            gated.append(task)
        else:
            gated.append(
                PlannedTask(
                    role=task.role,
                    description=task.description,
                    depends_on=sorted(set(task.depends_on) | {gate}),
                )
            )
    return ProjectPlan(tasks=gated)


def _validate_plan(plan: ProjectPlan) -> ProjectPlan:
    """Repair soft issues, reject hard ones, return a clean acyclic plan."""
    tasks = plan.tasks
    if not tasks:
        raise ValueError("plan has no tasks")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"plan has {len(tasks)} tasks (max {MAX_TASKS})")
    n = len(tasks)
    repaired: list[PlannedTask] = []
    for i, task in enumerate(tasks):
        if task.role not in PLAN_ROLES:
            raise ValueError(f"task {i} has unknown role {task.role!r}")
        # Drop out-of-range and self dependencies.
        deps = sorted({d for d in task.depends_on if 0 <= d < n and d != i})
        repaired.append(
            PlannedTask(role=task.role, description=task.description, depends_on=deps)
        )
    plan = ProjectPlan(tasks=repaired)
    plan = _gate_on_decision(plan)  # ensure an up-front decision blocks all other work
    topological_order(plan.tasks)  # raises ValueError if a cycle remains
    return plan


async def decompose(goal: str, *, client: Optional[AsyncOpenAI] = None) -> ProjectPlan:
    """Decompose a project goal into a validated, acyclic ProjectPlan."""
    client = client or _get_client()
    resp = await client.chat.completions.create(
        model=pm_model(),
        max_tokens=2048,
        messages=[
            {"role": "system", "content": ROLE_PROMPTS["pm"] + _DECOMPOSE_GUIDANCE},
            {"role": "user", "content": goal},
        ],
        tools=[_PLAN_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_plan"}},
    )
    usage.record(resp)
    plan = ProjectPlan.model_validate(tool_args(resp))  # Pydantic guard
    return _validate_plan(plan)  # semantic repair / reject

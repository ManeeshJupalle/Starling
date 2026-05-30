"""PM planner: decompose(goal) -> ProjectPlan.

One structured call using the pm role prompt, turning a goal into a small, acyclic
task graph. Constraints are enforced hard (ARCHITECTURE.md §4): roles restricted to
the worker set, at most ``MAX_TASKS`` tasks, and ``depends_on`` given as indices into
the returned list. Out-of-range and self dependencies are *repaired* (dropped);
unknown roles, too many tasks, and cycles are *rejected* (raise ValueError).
"""

from __future__ import annotations

from typing import Any, Optional

from anthropic import AsyncAnthropic

from ..schemas import PlannedTask, ProjectPlan
from .roles import DEFAULT_MODEL, PLAN_ROLES, ROLE_PROMPTS

MAX_TASKS = 12

_DECOMPOSE_GUIDANCE = (
    "\n\nDecompose the user's goal into at most {max} concrete tasks.\n"
    "- Each task's 'role' must be one of: {roles}.\n"
    "- 'depends_on' lists the 0-based indices of other tasks in your list that must "
    "finish before this one. Leave it empty for tasks that can start immediately.\n"
    "- Prefer independent gathering/research tasks that can run in parallel, feeding a "
    "final task that synthesizes their outputs.\n"
    "- Use the 'pm' role for a task whose description is a question to ask the user, but "
    "only when the goal has a genuine decision or ambiguity that needs their input. "
    "Tasks that use the answer should depend on that 'pm' task.\n"
    "- The dependency graph must be acyclic."
).format(max=MAX_TASKS, roles=", ".join(PLAN_ROLES))

_PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the decomposed project plan as a list of tasks.",
    "input_schema": {
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
}

# Lazily-created shared client so importing this module needs no API key (e.g. tests).
_client: Optional[AsyncAnthropic] = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def _tool_use_input(resp: Any) -> dict[str, Any]:
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("PM did not return a tool_use block")


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
    topological_order(plan.tasks)  # raises ValueError if a cycle remains
    return plan


async def decompose(goal: str, *, client: Optional[AsyncAnthropic] = None) -> ProjectPlan:
    """Decompose a project goal into a validated, acyclic ProjectPlan."""
    client = client or _get_client()
    resp = await client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=2048,
        system=ROLE_PROMPTS["pm"] + _DECOMPOSE_GUIDANCE,
        tools=[_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_plan"},
        messages=[{"role": "user", "content": goal}],
    )
    plan = ProjectPlan.model_validate(_tool_use_input(resp))  # Pydantic guard
    return _validate_plan(plan)  # semantic repair / reject

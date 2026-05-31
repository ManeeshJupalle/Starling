"""Critic — a verify/reflect pass before a deliverable reaches the user (Phase I2).

The coordination engine can produce a confident but wrong or incomplete result (a weak
plan, a worker that drifted). Before a project's result is delivered, a critic agent
checks it against the goal and either approves it or returns one corrected version —
using only facts already in the draft, so it never fabricates. One bounded LLM call.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from .. import usage
from ..llm import tool_args
from ..schemas import Verdict
from .roles import active_model

_CRITIC_SYSTEM = (
    "You are the critic in a multi-agent assistant. You are given the user's GOAL and a "
    "DRAFT deliverable produced by other agents. Judge whether the draft adequately and "
    "correctly satisfies the goal.\n"
    "- If it does, set ok=true.\n"
    "- If it falls short in a way you can fix using ONLY information already in the draft "
    "(it's incomplete, unclear, doesn't actually answer the goal, or contradicts it), set "
    "ok=false, give a one-line 'reason', and put a corrected version in 'revised'. Never "
    "invent facts, figures, or claims of actions that aren't already in the draft.\n"
    "- If it's wrong in a way you cannot fix from the draft alone, set ok=false, explain "
    "in 'reason', and leave 'revised' null."
)

_CRITIC_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Approve the draft or return a corrected version.",
        "parameters": Verdict.model_json_schema(),
    },
}


async def critique(goal: str, draft: str, *, client: AsyncOpenAI) -> Verdict:
    """Check a draft deliverable against the goal; return a validated Verdict."""
    resp = await client.chat.completions.create(
        model=active_model(),
        max_tokens=1024,
        messages=[
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": f"GOAL:\n{goal}\n\nDRAFT:\n{draft}"},
        ],
        tools=[_CRITIC_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_verdict"}},
    )
    usage.record(resp)
    return Verdict.model_validate(tool_args(resp))

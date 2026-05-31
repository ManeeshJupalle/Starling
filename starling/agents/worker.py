"""Stateless worker: an agentic tool-use loop that can pause for human approval.

``run_task`` builds a prompt from the role's system prompt + inputs and runs a tool
loop: the model may call tools, which are executed and fed back, until a final answer.
When a *sensitive* (state-changing) tool is requested and sensitive tools are allowed,
the loop PAUSES and returns a checkpoint instead of acting — the orchestrator stores
it, asks the user, and continues via ``resume_task`` on approval. With no tools it
collapses to a single call -> text. See ARCHITECTURE.md §2.5, §8.3, §8.5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AsyncOpenAI

from ..llm import make_client, text_of
from ..tools.base import ToolRegistry, is_read_only
from .roles import ROLE_PROMPTS, active_model

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = make_client()  # reads LLM_API_KEY / LLM_BASE_URL from the environment
    return _client


@dataclass
class WorkerResult:
    """Either a finished answer, or a pause awaiting approval of a tool call."""

    done: bool
    output: str = ""
    # When paused for approval (done is False):
    messages: list = field(default_factory=list)   # checkpoint: the conversation so far
    remaining: list = field(default_factory=list)  # tool calls not yet run; remaining[0] needs approval
    question: str = ""                             # the approval prompt for the user


def _build_user_prompt(description: str, inputs: dict[str, Any]) -> str:
    if not inputs:
        return description
    return f"{description}\n\nUpstream results to build on:\n{json.dumps(inputs, indent=2)}"


def _assistant_turn(message: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in message.tool_calls
        ],
    }


def _norm_call(tc: Any) -> dict[str, Any]:
    return {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}


async def _safe_call(tools: ToolRegistry, call: dict[str, Any]) -> str:
    try:
        return await tools.call(call["name"], json.loads(call["arguments"] or "{}"))
    except Exception as exc:  # surface tool failures back to the model
        return f"error: {exc}"


async def _answer_calls(messages, calls, tools, allow_sensitive, role) -> Optional[WorkerResult]:
    """Run tool calls in order; pause on the first sensitive one (if allowed).

    Returns a paused WorkerResult, or None when every call has been executed.
    """
    for i, call in enumerate(calls):
        if allow_sensitive and not is_read_only(call["name"]):
            return WorkerResult(
                done=False, messages=messages, remaining=calls[i:],
                question=f"The agent wants to run {call['name']}({call['arguments'][:160]}). "
                         "Approve? (yes/no)",
            )
        print(f"[worker:{role}] tool {call['name']} {call['arguments'][:80]}")
        messages.append({"role": "tool", "tool_call_id": call["id"],
                         "content": await _safe_call(tools, call)})
    return None


async def _loop(messages, *, client, tools, allow_sensitive, role, max_tokens, max_steps) -> WorkerResult:
    for _ in range(max_steps):
        kwargs: dict[str, Any] = {"model": active_model(), "max_tokens": max_tokens, "messages": messages}
        if tools:
            kwargs["tools"] = tools.openai_defs()
        resp = await client.chat.completions.create(**kwargs)
        message = resp.choices[0].message
        if not getattr(message, "tool_calls", None):
            return WorkerResult(done=True, output=text_of(resp))
        messages.append(_assistant_turn(message))
        calls = [_norm_call(tc) for tc in message.tool_calls]
        paused = await _answer_calls(messages, calls, tools, allow_sensitive, role)
        if paused is not None:
            return paused
    return WorkerResult(done=True, output="(stopped after the tool-step limit)")


async def run_task(
    role: str,
    description: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[ToolRegistry] = None,
    allow_sensitive: bool = False,
    max_tokens: int = 1024,
    max_steps: int = 6,
) -> WorkerResult:
    """Run a task; may pause for approval if ``allow_sensitive`` and a write tool is called."""
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unknown role: {role!r}")
    client = client or _get_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ROLE_PROMPTS[role]},
        {"role": "user", "content": _build_user_prompt(description, inputs or {})},
    ]
    return await _loop(messages, client=client, tools=tools, allow_sensitive=allow_sensitive,
                       role=role, max_tokens=max_tokens, max_steps=max_steps)


async def resume_task(
    role: str,
    messages: list,
    remaining: list,
    approved: bool,
    *,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[ToolRegistry] = None,
    max_tokens: int = 1024,
    max_steps: int = 6,
) -> WorkerResult:
    """Continue a paused worker: run (or skip) the approved tool call, then loop on."""
    client = client or _get_client()
    call = remaining[0]
    if approved:
        print(f"[worker:{role}] APPROVED {call['name']}")
        result = await _safe_call(tools, call)
    else:
        print(f"[worker:{role}] DENIED {call['name']}")
        result = "The user declined to approve this action. Do not retry it; continue without it."
    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    paused = await _answer_calls(messages, remaining[1:], tools, True, role)
    if paused is not None:
        return paused
    return await _loop(messages, client=client, tools=tools, allow_sensitive=True,
                       role=role, max_tokens=max_tokens, max_steps=max_steps)

"""Stateless worker: an agentic tool-use loop that can pause for human approval.

``run_task`` builds a prompt from the role's system prompt + inputs and runs a tool
loop: the model may call tools, which are executed and fed back, until a final answer.
When a *sensitive* (state-changing) tool is requested and sensitive tools are allowed,
the loop PAUSES and returns a checkpoint instead of acting — the orchestrator stores
it, asks the user, and continues via ``resume_task`` on approval. With no tools it
collapses to a single call -> text. See ARCHITECTURE.md §2.5, §8.3, §8.5.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AsyncOpenAI

from .. import usage
from ..llm import make_client, text_of
from ..tools.base import ToolRegistry, is_read_only
from .roles import ROLE_PROMPTS, active_model

# Tool reliability (Phase F): bound each tool call, and retry transient failures of
# read-only tools — never blindly retry a state-changing action.
TOOL_TIMEOUT = 30.0
TOOL_RETRIES = 2
TOOL_BACKOFF = 0.5

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


async def _safe_call(
    tools: ToolRegistry,
    call: dict[str, Any],
    *,
    timeout: float = TOOL_TIMEOUT,
    retries: int = TOOL_RETRIES,
    base_delay: float = TOOL_BACKOFF,
) -> str:
    """Run a tool with a timeout; retry transient failures of read-only tools."""
    name = call["name"]
    try:
        args = json.loads(call["arguments"] or "{}")
    except Exception as exc:
        return f"error: invalid arguments for {name}: {exc}"
    safe = tools.is_safe(name) if tools is not None else True
    attempts = retries + 1 if safe else 1  # don't retry state-changing actions
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncio.wait_for(tools.call(name, args), timeout=timeout)
        except Exception as exc:  # transient error or timeout
            last = exc
            if i < attempts - 1:
                print(f"[worker] tool {name} failed (attempt {i + 1}/{attempts}): {exc}; retrying")
                await asyncio.sleep(base_delay * (2 ** i))
    return f"error: {name} failed after {attempts} attempt(s): {last}"


async def _answer_calls(messages, calls, tools, allow_sensitive, role) -> Optional[WorkerResult]:
    """Run tool calls in order; pause on the first sensitive one (if allowed).

    Returns a paused WorkerResult, or None when every call has been executed.
    """
    for i, call in enumerate(calls):
        safe = tools.is_safe(call["name"]) if tools is not None else is_read_only(call["name"])
        if allow_sensitive and not safe:
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
        usage.record(resp)
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
    memory: str = "",
    max_tokens: int = 1024,
    max_steps: int = 6,
) -> WorkerResult:
    """Run a task; may pause for approval if ``allow_sensitive`` and a write tool is called.

    ``memory`` (if given) is injected as user-context the worker should honor.
    """
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unknown role: {role!r}")
    client = client or _get_client()
    messages: list[dict[str, Any]] = [{"role": "system", "content": ROLE_PROMPTS[role]}]
    if memory:
        messages.append({"role": "system", "content": f"What you know about the user (honor it):\n{memory}"})
    messages.append({"role": "user", "content": _build_user_prompt(description, inputs or {})})
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

"""Stateless worker: run_task(role, description, inputs) -> str.

Builds a prompt from the role's system prompt plus the task description and any
upstream inputs, then runs an agentic tool-use loop: the model may call tools, which
are executed here and fed back, until it returns a final answer. With no tools it
collapses to a single call -> text (the original behaviour). No durable state lives
here — it all lives in the blackboard. See ARCHITECTURE.md §2.5 and §8.3.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openai import AsyncOpenAI

from ..llm import make_client, text_of
from ..tools.base import ToolRegistry
from .roles import ROLE_PROMPTS, active_model

# Lazily-created shared client so importing this module needs no API key (e.g. for
# tests). Callers may inject their own client; the orchestrator passes one in.
_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = make_client()  # reads LLM_API_KEY / LLM_BASE_URL from the environment
    return _client


def _build_user_prompt(description: str, inputs: dict[str, Any]) -> str:
    if not inputs:
        return description
    return f"{description}\n\nUpstream results to build on:\n{json.dumps(inputs, indent=2)}"


def _assistant_turn(message: Any) -> dict[str, Any]:
    """Re-encode the assistant message that requested tools, to send back to the model."""
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ],
    }


async def run_task(
    role: str,
    description: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[ToolRegistry] = None,
    max_tokens: int = 1024,
    max_steps: int = 6,
) -> str:
    """Run one task for ``role`` and return the model's text output.

    If ``tools`` are provided, the model may call them in a loop (executed here, results
    fed back) for up to ``max_steps`` rounds before producing a final answer.
    """
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unknown role: {role!r}")
    client = client or _get_client()
    tool_defs = tools.openai_defs() if tools else None

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ROLE_PROMPTS[role]},
        {"role": "user", "content": _build_user_prompt(description, inputs or {})},
    ]

    for _ in range(max_steps):
        kwargs: dict[str, Any] = {
            "model": active_model(),
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if tool_defs:
            kwargs["tools"] = tool_defs
        resp = await client.chat.completions.create(**kwargs)
        message = resp.choices[0].message

        if not getattr(message, "tool_calls", None):
            return text_of(resp)  # final answer

        # The model asked to use tools: record its turn, run them, feed results back.
        messages.append(_assistant_turn(message))
        for tc in message.tool_calls:
            print(f"[worker:{role}] tool {tc.function.name} {(tc.function.arguments or '')[:80]}")
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = await tools.call(tc.function.name, args)
            except Exception as exc:  # surface tool failures back to the model
                result = f"error: {exc}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "(stopped after the tool-step limit without a final answer)"

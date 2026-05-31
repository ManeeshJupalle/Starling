"""Stateless worker: run_task(role, description, inputs) -> str.

Builds a prompt from the role's system prompt plus the task description and any
upstream inputs, calls the model, and returns the text. No durable state lives here
— it all lives in the blackboard. See ARCHITECTURE.md §2.5.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openai import AsyncOpenAI

from ..llm import make_client, text_of
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


async def run_task(
    role: str,
    description: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    client: Optional[AsyncOpenAI] = None,
    max_tokens: int = 1024,
) -> str:
    """Run one task for ``role`` and return the model's text output."""
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unknown role: {role!r}")
    client = client or _get_client()
    resp = await client.chat.completions.create(
        model=active_model(),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": ROLE_PROMPTS[role]},
            {"role": "user", "content": _build_user_prompt(description, inputs or {})},
        ],
    )
    return text_of(resp)

"""Thin helpers over the OpenAI-compatible chat API.

The provider is chosen entirely by ``base_url`` + key (OpenRouter, Groq, OpenAI, …),
so nothing else in the codebase knows or cares which one is behind it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from openai import AsyncOpenAI


def make_client(api_key: Optional[str] = None, base_url: Optional[str] = None) -> AsyncOpenAI:
    """Build an OpenAI-compatible async client, falling back to env vars."""
    return AsyncOpenAI(
        api_key=api_key or os.environ.get("LLM_API_KEY"),
        base_url=(base_url or os.environ.get("LLM_BASE_URL")) or None,
    )


def text_of(resp: Any) -> str:
    """Extract the assistant's text from a chat completion."""
    return (resp.choices[0].message.content or "").strip()


def tool_args(resp: Any) -> dict[str, Any]:
    """Parse the arguments of the first tool/function call as a dict."""
    tool_calls = resp.choices[0].message.tool_calls
    if not tool_calls:
        raise ValueError("model did not return a tool/function call")
    return json.loads(tool_calls[0].function.arguments)

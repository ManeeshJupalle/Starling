"""Environment configuration for Starling.

Loads a local ``.env`` (if present) and reads required secrets + tunables from the
environment. Importing this module fails fast with a clear message when a required
variable is missing, so misconfiguration surfaces at startup rather than mid-run.

The LLM provider is OpenAI-compatible and selected entirely via env vars, so Starling
can point at OpenRouter, Groq, OpenAI, or any compatible endpoint without code
changes — see ``.env.example``.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from a local .env file if one exists


def _require(name: str) -> str:
    """Return the value of a required environment variable or raise."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill it in."
        )
    return value


# LLM provider (OpenAI-compatible) — key required; base URL has an OpenRouter default.
LLM_API_KEY: str = _require("LLM_API_KEY")
LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")

# Telegram — required.
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# Scheduler heartbeat in seconds — optional, defaults to 5.
TICK_INTERVAL: int = int(os.environ.get("TICK_INTERVAL", "5"))

# Live web dashboard port — optional, defaults to 8000.
DASHBOARD_PORT: int = int(os.environ.get("DASHBOARD_PORT", "8000"))

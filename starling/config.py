"""Environment configuration for Starling.

Reads required secrets and tunables from the process environment. Importing this
module fails fast with a clear message when a required variable is missing, so
misconfiguration surfaces at startup rather than mid-run.

Copy ``.env.example`` to ``.env`` and export the values (or set them in your
shell) before running anything.
"""

from __future__ import annotations

import os


def _require(name: str) -> str:
    """Return the value of a required environment variable or raise."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill it in, then export the values."
        )
    return value


# Secrets — required.
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# Scheduler heartbeat in seconds — optional, defaults to 5.
TICK_INTERVAL: int = int(os.environ.get("TICK_INTERVAL", "5"))

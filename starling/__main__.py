"""Wiring + entrypoint.

Runs the Telegram channel with the orchestrator handling each message — ephemeral
mode (classify -> parallel fan-out -> merge) and project mode (decompose -> persist
-> schedule) — and the scheduler running concurrently in the same event loop, driving
tasks to completion and posting results back.

    python -m starling
"""

from __future__ import annotations

import os
import sys

from .blackboard import DEFAULT_DB_PATH, Blackboard
from .channels.telegram import TelegramChannel
from .llm import make_client
from .orchestrator import Orchestrator
from .scheduler import Scheduler


def main() -> None:
    # Deferred so importing this module (e.g. for tests) doesn't require secrets;
    # config loads .env and fails fast on missing env vars the moment we actually run.
    from . import config

    # `python -m starling --reset` wipes the blackboard for a clean run. Stop any
    # running instance first (the DB file is locked while it runs).
    if "--reset" in sys.argv:
        if os.path.exists(DEFAULT_DB_PATH):
            try:
                os.remove(DEFAULT_DB_PATH)
                print(f"[reset] wiped {DEFAULT_DB_PATH}")
            except OSError as exc:
                print(f"[reset] could not remove {DEFAULT_DB_PATH}: {exc}")
                print("[reset] stop any running Starling first, then retry.")
                return
        else:
            print(f"[reset] no {DEFAULT_DB_PATH} found")

    channel = TelegramChannel(config.TELEGRAM_BOT_TOKEN)
    client = make_client(config.LLM_API_KEY, config.LLM_BASE_URL)
    blackboard = Blackboard()
    scheduler = Scheduler(blackboard, channel, client, config.TICK_INTERVAL)
    orchestrator = Orchestrator(channel, client, blackboard, scheduler)
    channel.on_message(orchestrator.handle_message)

    print("Starling running (ephemeral + project mode, scheduler active). Ctrl+C to stop.")
    channel.run(on_start=scheduler.run)


if __name__ == "__main__":
    main()

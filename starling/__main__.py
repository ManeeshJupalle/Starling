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
from .channels.web import WebDashboard
from .llm import make_client
from .orchestrator import Orchestrator
from .scheduler import Scheduler
from .tools.mcp import MCPManager


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
    tools_manager = MCPManager()
    scheduler = Scheduler(blackboard, channel, client, config.TICK_INTERVAL, tools_manager=tools_manager)
    orchestrator = Orchestrator(channel, client, blackboard, scheduler, tools_manager=tools_manager)
    scheduler.on_trigger = orchestrator.run_goal  # let due triggers start projects/answers

    async def _poll_tool(name: str, args: dict) -> str:
        """Call one MCP read tool by name (used by inbox-watch triggers)."""
        registry = tools_manager.registry_for([name.split("__", 1)[0]], include_sensitive=True)
        return await registry.call(name, args)

    scheduler.poll_tool = _poll_tool
    channel.on_message(orchestrator.handle_message)
    dashboard = WebDashboard(blackboard, port=config.DASHBOARD_PORT)

    async def _startup() -> None:
        # Connect MCP servers, start the dashboard, then run the scheduler loop. Tools
        # and the dashboard are best-effort; the bot still runs if either fails.
        try:
            await tools_manager.start()
        except Exception as exc:
            print(f"[mcp] startup failed (continuing without tools): {exc}")
        try:
            await dashboard.start()
        except Exception as exc:
            print(f"[web] dashboard failed to start: {exc}")
        await scheduler.run()

    print("Starling running (MCP tools + project mode + web dashboard). Ctrl+C to stop.")
    channel.run(on_start=_startup)


if __name__ == "__main__":
    main()

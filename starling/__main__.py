"""Wiring + entrypoint.

Phase 5: run the Telegram channel with the orchestrator handling each message —
ephemeral mode end-to-end, plus project mode that decomposes a goal and persists the
task graph — and the scheduler running concurrently in the same event loop, driving
those tasks to completion and posting results back.

    python -m starling
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from .blackboard import Blackboard
from .channels.telegram import TelegramChannel
from .orchestrator import Orchestrator
from .scheduler import Scheduler


def main() -> None:
    # Deferred so importing this module (e.g. for tests) doesn't require secrets;
    # config fails fast on missing env vars the moment we actually run.
    from . import config

    channel = TelegramChannel(config.TELEGRAM_BOT_TOKEN)
    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    blackboard = Blackboard()
    scheduler = Scheduler(blackboard, channel, client, config.TICK_INTERVAL)
    orchestrator = Orchestrator(channel, client, blackboard, scheduler)
    channel.on_message(orchestrator.handle_message)

    print("Starling running (ephemeral + project mode, scheduler active). Ctrl+C to stop.")
    channel.run(on_start=scheduler.run)


if __name__ == "__main__":
    main()

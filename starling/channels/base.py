"""Channel interface.

The pluggable contract every chat backend implements. A Channel adapts a concrete
platform (Telegram first; Discord later) to two operations — register an inbound
handler, push outbound text — and holds **zero** orchestration logic. Adding a
backend means writing another subclass, nothing else. See ARCHITECTURE.md §2.1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

# An inbound handler receives (chat_id, text) and does its work, optionally replying
# via Channel.send. Async so handlers can await I/O (model calls, outbound sends).
InboundHandler = Callable[[int, str], Awaitable[None]]

# An optional startup coroutine scheduled as a background task once the event loop is
# running — used to run the scheduler alongside the channel without blocking it.
StartupHook = Callable[[], Awaitable[None]]


class Channel(ABC):
    """Transport-agnostic chat interface."""

    @abstractmethod
    def on_message(self, handler: InboundHandler) -> None:
        """Register the async handler invoked with (chat_id, text) per inbound message."""

    @abstractmethod
    async def send(self, chat_id: int, text: str) -> None:
        """Send a text message to a chat."""

    @abstractmethod
    def run(self, on_start: Optional[StartupHook] = None) -> None:
        """Start receiving messages and block until the process is stopped.

        ``on_start``, if given, is scheduled as a background task once the event loop
        is running (the channel runs it concurrently; it does not await completion).
        """

"""Scratch verification for Phase 1 (channel adapter + echo loop).

Exercises the Channel contract through a fake in-memory backend, so it runs without
a Telegram token: register a trivial echo handler, simulate inbound messages, and
confirm each is sent straight back to the same chat.

The live check ("message the bot on Telegram, it echoes") was run separately with a
real TELEGRAM_BOT_TOKEN. (From Phase 3 onward the live bot orchestrates rather than
echoes; this test still pins down the adapter's register -> deliver -> send contract.)
"""

import asyncio

from starling.channels.base import Channel, InboundHandler


def make_echo_handler(channel: Channel) -> InboundHandler:
    """A trivial handler that echoes each inbound message back to its chat."""

    async def echo(chat_id: int, text: str) -> None:
        await channel.send(chat_id, text)

    return echo


class FakeChannel(Channel):
    """In-memory Channel that records what would be sent outbound."""

    def __init__(self) -> None:
        self._handler: InboundHandler | None = None
        self.sent: list[tuple[int, str]] = []

    def on_message(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def run(self) -> None:  # unused in this test
        raise NotImplementedError

    async def deliver(self, chat_id: int, text: str) -> None:
        """Simulate an inbound message arriving from the platform."""
        assert self._handler is not None, "no handler registered"
        await self._handler(chat_id, text)


async def main() -> None:
    channel = FakeChannel()
    channel.on_message(make_echo_handler(channel))

    await channel.deliver(42, "hello starling")
    await channel.deliver(42, "second message")

    print("inbound -> outbound:")
    for chat_id, text in channel.sent:
        print(f"  chat {chat_id}: {text!r}")

    assert channel.sent == [(42, "hello starling"), (42, "second message")]
    print("\nPASS: handler echoes each inbound message back to the same chat.")


if __name__ == "__main__":
    asyncio.run(main())

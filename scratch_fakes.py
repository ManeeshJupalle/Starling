"""Shared fakes for the offline scratch verification scripts (no API key / Telegram).

Provides a fake Channel that records outbound messages, and minimal OpenAI-shaped
chat-completion responses so a FakeClient can stand in for AsyncOpenAI.
"""

from __future__ import annotations

import json

from starling.channels.base import Channel, InboundHandler


class FakeChannel(Channel):
    """In-memory Channel that records what would be sent outbound."""

    def __init__(self) -> None:
        self._handler: InboundHandler | None = None
        self.sent: list[tuple[int, str]] = []

    def on_message(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def run(self, on_start=None) -> None:
        raise NotImplementedError

    async def deliver(self, chat_id: int, text: str) -> None:
        """Simulate an inbound message arriving from the platform."""
        assert self._handler is not None, "no handler registered"
        await self._handler(chat_id, text)

    def texts(self) -> list[str]:
        return [t for _, t in self.sent]


# --- OpenAI-shaped chat responses -----------------------------------------

class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, name: str, arguments: str, id: str = "call_0") -> None:
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Msg) -> None:
        self.message = message


class _Resp:
    def __init__(self, message: _Msg) -> None:
        self.choices = [_Choice(message)]


def text_response(text: str) -> _Resp:
    return _Resp(_Msg(content=text))


def tool_response(name: str, args: dict) -> _Resp:
    return _Resp(_Msg(tool_calls=[_ToolCall(name, json.dumps(args))]))


class _Completions:
    def __init__(self, create) -> None:
        self._create = create

    async def create(self, **kw):
        return await self._create(**kw)


class _Chat:
    def __init__(self, create) -> None:
        self.completions = _Completions(create)


def chat(create):
    """Wrap an async ``create(**kw)`` callable as a fake ``.chat.completions`` namespace."""
    return _Chat(create)


def system_of(kw) -> str:
    return kw["messages"][0]["content"]


def user_of(kw) -> str:
    return kw["messages"][-1]["content"]


def tool_name(kw) -> str:
    tools = kw.get("tools") or []
    return tools[0]["function"]["name"] if tools else ""

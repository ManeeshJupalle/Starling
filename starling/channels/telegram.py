"""Telegram channel adapter (python-telegram-bot, v20+ async API).

The first concrete Channel backend: simplest bot API, fastest demo. Pure transport
— inbound text becomes an (chat_id, text) call to the registered handler, and the
handler's replies go out via ``send``. No classification, blackboard, or model
calls live here. See ARCHITECTURE.md §2.1.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler as TgMessageHandler,
    filters,
)

from .base import Channel, InboundHandler, StartupHook


class TelegramChannel(Channel):
    """Adapts python-telegram-bot to the :class:`Channel` interface."""

    def __init__(self, token: str) -> None:
        self._handler: Optional[InboundHandler] = None
        self._on_start: Optional[StartupHook] = None
        self._app = (
            Application.builder().token(token).post_init(self._post_init).build()
        )
        # Route plain text (not slash-commands) through our dispatcher.
        self._app.add_handler(
            TgMessageHandler(filters.TEXT & ~filters.COMMAND, self._dispatch)
        )

    def on_message(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, chat_id: int, text: str) -> None:
        await self._app.bot.send_message(chat_id=chat_id, text=text)

    def run(self, on_start: Optional[StartupHook] = None) -> None:
        self._on_start = on_start
        self._app.run_polling()

    async def _post_init(self, app: Application) -> None:
        # Runs once inside the event loop, before polling begins. Launch the startup
        # hook (e.g. the scheduler) as a concurrent background task in the same loop.
        if self._on_start is not None:
            asyncio.create_task(self._on_start())

    async def _dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Translate a Telegram update into a (chat_id, text) handler call."""
        if self._handler is None or update.message is None or update.effective_chat is None:
            return
        await self._handler(update.effective_chat.id, update.message.text or "")

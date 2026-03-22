"""
XClaw Telegram Interface — connects python-telegram-bot to the Gateway.

Setup:
  1. Create a bot via @BotFather, copy the token to TELEGRAM_BOT_TOKEN in .env
  2. Run: python -m interface.telegram  (or via main.py)

Each Telegram user gets their own session_id derived from their chat_id.
Inline keyboard buttons are used for the Approve / Cancel flow.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.gateway import Gateway

logger = logging.getLogger(__name__)

# Approval callback data constants
_APPROVE = "xclaw:approve"
_CANCEL = "xclaw:cancel"


def build_telegram_app(gateway: "Gateway"):
    """
    Build and return a configured python-telegram-bot Application.

    Returns None if python-telegram-bot is not installed, so XClaw can still
    start without the Telegram interface.
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )
    except ImportError:
        logger.warning("python-telegram-bot not installed — Telegram interface disabled.")
        return None

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram interface disabled.")
        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "👋 XClaw online. Tell me what you need and I'll build a plan.\n\n"
            "Examples:\n"
            "• Research competitors of Harver Space\n"
            "• Write a cold email to SpaceX\n"
            "• Show my task list\n"
            "• Get BTC price"
        )

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        session_id = str(update.effective_chat.id)
        text = update.message.text or ""

        response = await gateway.handle(text, "telegram", session_id)

        if response.requires_approval:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=_APPROVE),
                    InlineKeyboardButton("❌ Cancel", callback_data=_CANCEL),
                ]
            ])
            await update.message.reply_text(response.text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await update.message.reply_text(response.text, parse_mode="Markdown")

    async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        session_id = str(update.effective_chat.id)

        answer = "yes" if query.data == _APPROVE else "no"
        response = await gateway.handle(answer, "telegram", session_id)
        await query.edit_message_text(response.text, parse_mode="Markdown")

    async def handle_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram error: %s", ctx.error, exc_info=ctx.error)

    # ------------------------------------------------------------------
    # Wire up the application
    # ------------------------------------------------------------------

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(handle_error)

    return app


def run_telegram(gateway: "Gateway") -> None:
    """Start the Telegram bot (blocking)."""
    app = build_telegram_app(gateway)
    if app is None:
        return
    logger.info("Starting Telegram interface…")
    app.run_polling()

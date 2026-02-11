"""Telegram bot that bridges messages to Claude CLI."""

import asyncio
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from claude_runner import run_claude
from config import Config
from db import save_message
from formatting import (
    format_response,
    is_plain_text,
    is_text_content,
)

logger = logging.getLogger(__name__)


async def send_typing_periodically(
    chat_id: int, bot, stop_event: asyncio.Event
) -> None:
    """Send TYPING action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def send_chunks(update: Update, chunks: list) -> None:
    """Send formatted content chunks to the user."""
    for item in chunks:
        try:
            if is_plain_text(item):
                await update.message.reply_text(
                    item,
                    disable_web_page_preview=True,
                )
            elif is_text_content(item):
                await update.message.reply_text(
                    item.content,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
        except Exception as e:
            # Fallback: send as plain text without MarkdownV2
            logger.warning("Failed to send formatted chunk: %s", e)
            try:
                text = item if is_plain_text(item) else getattr(item, "content", str(item))
                await update.message.reply_text(text, disable_web_page_preview=True)
            except Exception as e2:
                logger.error("Failed to send chunk even as plain text: %s", e2)


async def handle_message(update: Update, context) -> None:
    """Handle incoming text messages: forward to Claude CLI and reply."""
    chat_id = update.message.chat_id
    user_text = update.message.text
    config: Config = context.bot_data["config"]

    logger.info("Message received from chat_id=%d (%d chars)", chat_id, len(user_text))
    save_message("user", user_text, source="telegram")

    # Start typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    try:
        response = await run_claude(user_text, config)
    except TimeoutError:
        await update.message.reply_text(
            "Timeout : Claude n'a pas répondu en 120 secondes."
        )
        return
    except RuntimeError as e:
        logger.error("Claude error: %s", e)
        await update.message.reply_text(f"Erreur Claude : {e}")
        return
    except Exception as e:
        logger.error("Unexpected error running Claude: %s", e)
        await update.message.reply_text(f"Erreur inattendue : {type(e).__name__}")
        return
    finally:
        stop_typing.set()
        await typing_task

    logger.info("Claude responded (%d chars)", len(response))
    save_message("assistant", response, source="telegram")

    chunks = await format_response(response, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_start(update: Update, context) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Bot Claude CLI actif. Envoie un message et je le transmets à Claude."
    )


async def error_handler(update: object, context) -> None:
    """Global error handler: log and notify user if possible."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                "Une erreur interne est survenue. Vérifie les logs."
            )
        except Exception:
            pass


def main() -> None:
    """Initialize and run the bot."""
    load_dotenv()

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # Reduce noise from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = Config.from_env()

    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["config"] = config

    auth = filters.Chat(chat_id=config.allowed_chat_id)
    app.add_handler(CommandHandler("start", handle_start, filters=auth))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & auth, handle_message)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot started, polling for updates (chat_id=%d)...", config.allowed_chat_id)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

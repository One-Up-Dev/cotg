"""Telegram bot that bridges messages to Claude CLI."""

import asyncio
import logging
import os
import re
import time

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from claude_runner import run_claude, stream_claude
from config import Config
from db import save_message, save_fact, save_task, complete_task, get_active_facts, get_active_tasks, get_recent_summaries
from formatting import (
    format_response,
    is_plain_text,
    is_text_content,
    sanitize_output,
)

# Claude Code slash commands available from Telegram
CLAUDE_COMMANDS_DIR = os.path.expanduser("~/.claude/commands")
CLAUDE_COMMANDS = ["check", "test", "review", "learn", "workflow"]

logger = logging.getLogger(__name__)

_FACT_PATTERNS = [
    re.compile(
        r"(?:retiens?|remember|note|rappelle[- ]toi|n'oublie pas|oublie pas)\s+(?:que\s+)?(.+)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:toujours|always)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:jamais|never)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:je pr[eé]f[eè]re?|i prefer)\s+(.+)", re.IGNORECASE),
]


def _extract_facts_from_message(text: str) -> None:
    """Detect 'remember that...' patterns and persist as facts."""
    for pattern in _FACT_PATTERNS:
        match = pattern.search(text)
        if match:
            fact = match.group(1).strip().rstrip(".")
            if len(fact) > 10:
                save_fact(fact, category="preference", source="telegram")
                logger.info("Fact extracted: %s", fact[:80])
                break


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
    _extract_facts_from_message(user_text)

    # Start typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    name = config.assistant_name
    try:
        response = await run_claude(user_text, config)
    except TimeoutError:
        await update.message.reply_text(
            f"Timeout : {name} n'a pas répondu en {config.claude_timeout} secondes."
        )
        return
    except RuntimeError as e:
        logger.error("%s error: %s", name, e)
        await update.message.reply_text(f"Erreur {name} : {e}")
        return
    except Exception as e:
        logger.error("Unexpected error running %s: %s", name, e)
        await update.message.reply_text(f"Erreur inattendue : {type(e).__name__}")
        return
    finally:
        stop_typing.set()
        await typing_task

    logger.info("%s responded (%d chars)", name, len(response))
    save_message("assistant", response, source="telegram")

    chunks = await format_response(response, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_message_streaming(update: Update, context) -> None:
    """Handle incoming messages with real-time streaming to Telegram."""
    chat_id = update.message.chat_id
    user_text = update.message.text
    config: Config = context.bot_data["config"]

    # Fallback to batch mode if streaming disabled
    if not config.stream_enabled:
        return await handle_message(update, context)

    logger.info("Message received (stream) from chat_id=%d (%d chars)", chat_id, len(user_text))
    save_message("user", user_text, source="telegram")
    _extract_facts_from_message(user_text)

    # Start typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    # Send initial placeholder message
    streaming_msg = await update.message.reply_text(
        "...",
        disable_web_page_preview=True,
    )

    buffer = ""
    last_edit = 0.0
    display_limit = 4000  # Truncate display during stream, keep full buffer

    try:
        async for chunk in stream_claude(user_text, config):
            if chunk is None:
                # Stream complete
                break
            buffer += chunk

            now = time.monotonic()
            if now - last_edit >= config.stream_edit_interval:
                display = buffer[:display_limit]
                if len(buffer) > display_limit:
                    display += "\n\n... (streaming)"
                try:
                    await streaming_msg.edit_text(
                        display + config.stream_indicator,
                        disable_web_page_preview=True,
                    )
                    last_edit = now
                except Exception as e:
                    logger.warning("Stream edit failed: %s", e)

    except TimeoutError:
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Timeout : {config.assistant_name} n'a pas répondu à temps.")
        except Exception:
            pass
        return
    except RuntimeError as e:
        logger.error("Stream error: %s", e)
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Erreur {config.assistant_name} : {e}")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error("Unexpected stream error: %s", e)
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Erreur inattendue : {type(e).__name__}")
        except Exception:
            pass
        return
    finally:
        stop_typing.set()
        await typing_task

    if not buffer.strip():
        try:
            await streaming_msg.edit_text(f"(Réponse vide de {config.assistant_name})")
        except Exception:
            pass
        return

    logger.info("Stream complete (%d chars)", len(buffer))
    save_message("assistant", buffer, source="telegram")

    # Delete the streaming message, send properly formatted response
    try:
        await streaming_msg.delete()
    except Exception:
        pass

    buffer = sanitize_output(buffer)
    chunks = await format_response(buffer, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_claude_command(update: Update, context) -> None:
    """Handle Claude Code slash commands (/check, /test, /review, etc.).

    Reads the command .md file, substitutes $ARGUMENTS, and sends to Claude.
    """
    chat_id = update.message.chat_id
    config: Config = context.bot_data["config"]

    # Extract command name from /command text
    text = update.message.text or ""
    parts = text.split(None, 1)
    cmd_name = parts[0].lstrip("/").lower()
    arguments = parts[1] if len(parts) > 1 else ""

    # Read the command file
    cmd_file = os.path.join(CLAUDE_COMMANDS_DIR, f"{cmd_name}.md")
    if not os.path.isfile(cmd_file):
        await update.message.reply_text(
            f"Commande /{cmd_name} introuvable.",
            disable_web_page_preview=True,
        )
        return

    with open(cmd_file) as f:
        prompt = f.read()

    # Substitute $ARGUMENTS placeholder
    prompt = prompt.replace("$ARGUMENTS", arguments)

    logger.info("Claude command /%s from chat_id=%d (args=%r)", cmd_name, chat_id, arguments)
    save_message("user", f"/{cmd_name} {arguments}".strip(), source="telegram")

    # Use streaming handler logic
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        send_typing_periodically(chat_id, context.bot, stop_typing)
    )

    streaming_msg = await update.message.reply_text(
        f"Exécution de /{cmd_name}...",
        disable_web_page_preview=True,
    )

    buffer = ""
    last_edit = 0.0
    display_limit = 4000

    try:
        async for chunk in stream_claude(prompt, config):
            if chunk is None:
                break
            buffer += chunk

            now = time.monotonic()
            if now - last_edit >= config.stream_edit_interval:
                display = buffer[:display_limit]
                if len(buffer) > display_limit:
                    display += "\n\n... (streaming)"
                try:
                    await streaming_msg.edit_text(
                        display + config.stream_indicator,
                        disable_web_page_preview=True,
                    )
                    last_edit = now
                except Exception as e:
                    logger.warning("Stream edit failed: %s", e)

    except TimeoutError:
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(
                f"Timeout : /{cmd_name} n'a pas répondu à temps."
            )
        except Exception:
            pass
        return
    except (RuntimeError, Exception) as e:
        logger.error("Command /%s error: %s", cmd_name, e)
        stop_typing.set()
        await typing_task
        try:
            await streaming_msg.edit_text(f"Erreur /{cmd_name} : {e}")
        except Exception:
            pass
        return
    finally:
        stop_typing.set()
        await typing_task

    if not buffer.strip():
        try:
            await streaming_msg.edit_text(f"(Réponse vide pour /{cmd_name})")
        except Exception:
            pass
        return

    logger.info("Command /%s complete (%d chars)", cmd_name, len(buffer))
    save_message("assistant", buffer, source="telegram")

    try:
        await streaming_msg.delete()
    except Exception:
        pass

    buffer = sanitize_output(buffer)
    chunks = await format_response(buffer, config.max_message_length)
    await send_chunks(update, chunks)


async def handle_start(update: Update, context) -> None:
    """Handle /start command."""
    name = context.bot_data["config"].assistant_name
    await update.message.reply_text(
        f"Salut ! Je suis {name}, ton assistant personnel. Envoie-moi un message !",
        disable_web_page_preview=True,
    )


async def handle_memory(update: Update, context) -> None:
    """Handle /memory command — show memory status."""
    facts = get_active_facts()
    tasks = get_active_tasks()
    summaries = get_recent_summaries(limit=3)

    lines = ["**Etat de la mémoire**\n"]

    # Facts
    lines.append(f"**Faits persistants** ({len(facts)}):")
    if facts:
        for cat, content in facts:
            lines.append(f"  [{cat}] {content}")
    else:
        lines.append("  (aucun)")

    # Tasks
    lines.append(f"\n**Tâches actives** ({len(tasks)}):")
    if tasks:
        for title, status, ctx, _ in tasks:
            icon = {"pending": "[ ]", "in_progress": "[>]", "blocked": "[!]"}.get(status, "[ ]")
            lines.append(f"  {icon} {title}")
    else:
        lines.append("  (aucune)")

    # Summaries
    lines.append(f"\n**Résumés récents** ({len(summaries)}):")
    if summaries:
        for summary, decisions, files, created_at in summaries:
            date = created_at[:10] if created_at else "?"
            lines.append(f"  [{date}] {summary[:150]}")
    else:
        lines.append("  (aucun)")

    await update.message.reply_text(
        "\n".join(lines),
        disable_web_page_preview=True,
    )


async def handle_remember(update: Update, context) -> None:
    """Handle /remember <fact> — save a persistent fact."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /remember <fait à retenir>",
            disable_web_page_preview=True,
        )
        return
    fact = " ".join(context.args)
    save_fact(fact, category="user_note", source="telegram")
    await update.message.reply_text(
        f"Retenu : {fact}",
        disable_web_page_preview=True,
    )


async def handle_tasks(update: Update, context) -> None:
    """Handle /tasks command — manage tasks.

    Usage:
      /tasks            — list active tasks
      /tasks add <text> — add a new task
      /tasks done <text> — mark a task as done
    """
    args = context.args or []

    if not args:
        tasks = get_active_tasks()
        if not tasks:
            await update.message.reply_text(
                "Aucune tâche active.",
                disable_web_page_preview=True,
            )
            return
        lines = ["**Tâches actives**\n"]
        icons = {"pending": "[ ]", "in_progress": "[>]", "blocked": "[!]"}
        for title, status, ctx, updated_at in tasks:
            icon = icons.get(status, "[ ]")
            lines.append(f"  {icon} {title}")
        await update.message.reply_text(
            "\n".join(lines),
            disable_web_page_preview=True,
        )
        return

    action = args[0].lower()
    text = " ".join(args[1:])

    if action == "add" and text:
        save_task(text, status="pending")
        await update.message.reply_text(
            f"Tâche ajoutée : {text}",
            disable_web_page_preview=True,
        )
    elif action == "done" and text:
        complete_task(text)
        await update.message.reply_text(
            f"Tâche terminée : {text}",
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "Usage:\n/tasks — lister\n/tasks add <texte> — ajouter\n/tasks done <texte> — terminer",
            disable_web_page_preview=True,
        )


async def handle_forget(update: Update, context) -> None:
    """Handle /forget <fact> — deactivate a persistent fact."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /forget <fait à oublier>",
            disable_web_page_preview=True,
        )
        return
    search = " ".join(context.args).lower()
    # Find and deactivate matching facts
    from db import get_connection, _init_memory_tables
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            rows = conn.execute(
                "SELECT id, content FROM facts WHERE active = 1"
            ).fetchall()
            deactivated = []
            for row in rows:
                if search in row["content"].lower():
                    conn.execute(
                        "UPDATE facts SET active = 0 WHERE id = ?", (row["id"],)
                    )
                    deactivated.append(row["content"])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to forget fact: %s", e)
        await update.message.reply_text("Erreur lors de la suppression.")
        return

    if deactivated:
        await update.message.reply_text(
            f"Oublié ({len(deactivated)}) : " + ", ".join(deactivated[:3]),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "Aucun fait correspondant trouvé.",
            disable_web_page_preview=True,
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


BOT_COMMANDS = [
    BotCommand("start", "Démarrer le bot"),
    BotCommand("memory", "État de la mémoire"),
    BotCommand("remember", "Retenir un fait"),
    BotCommand("forget", "Oublier un fait"),
    BotCommand("tasks", "Gérer les tâches"),
    BotCommand("check", "Lancer les checks qualité"),
    BotCommand("test", "Lancer les tests"),
    BotCommand("review", "Revue qualité du code"),
    BotCommand("learn", "Extraire les leçons"),
    BotCommand("workflow", "Orchestrer un workflow"),
]


async def post_init(application) -> None:
    """Set bot commands menu on startup."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Bot commands menu set (%d commands)", len(BOT_COMMANDS))


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

    app = Application.builder().token(config.telegram_token).post_init(post_init).build()
    app.bot_data["config"] = config

    auth = filters.Chat(chat_id=config.allowed_chat_id)
    app.add_handler(CommandHandler("start", handle_start, filters=auth))
    app.add_handler(CommandHandler("memory", handle_memory, filters=auth))
    app.add_handler(CommandHandler("remember", handle_remember, filters=auth))
    app.add_handler(CommandHandler("forget", handle_forget, filters=auth))
    app.add_handler(CommandHandler("tasks", handle_tasks, filters=auth))
    # Claude Code slash commands
    for cmd in CLAUDE_COMMANDS:
        app.add_handler(CommandHandler(cmd, handle_claude_command, filters=auth))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & auth, handle_message_streaming)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot started, polling for updates (chat_id=%d)...", config.allowed_chat_id)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

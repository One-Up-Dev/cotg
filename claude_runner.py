"""Async wrapper for claude CLI subprocess execution."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator

from config import Config
from db import get_active_facts, get_active_tasks, get_recent_summaries

logger = logging.getLogger(__name__)


def _claude_env() -> dict[str, str]:
    """Return env with writable TMPDIR."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    tmpdir = os.path.expanduser("~/tmp")
    os.makedirs(tmpdir, exist_ok=True)
    env["TMPDIR"] = tmpdir
    return env


def _build_memory_context() -> str:
    """Build memory context string from DB for injection into system prompt."""
    sections = []

    # Long-term facts
    facts = get_active_facts()
    if facts:
        lines = ["[Long-term memory]"]
        for category, content in facts:
            lines.append(f"  [{category}] {content}")
        sections.append("\n".join(lines))

    # Recent session summaries
    summaries = get_recent_summaries(limit=3)
    if summaries:
        lines = ["[Recent sessions]"]
        for summary, decisions, files_modified, created_at in summaries:
            date = created_at[:10] if created_at else "?"
            lines.append(f"  [{date}] {summary[:200]}")
            if files_modified:
                lines.append(f"    Files: {files_modified}")
        sections.append("\n".join(lines))

    # Active tasks
    tasks = get_active_tasks()
    if tasks:
        lines = ["[Active tasks]"]
        icons = {"pending": "[ ]", "in_progress": "[>]", "blocked": "[!]"}
        for title, status, context, _ in tasks:
            icon = icons.get(status, "[ ]")
            lines.append(f"  {icon} {title}")
        sections.append("\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(sections)


async def run_claude(message: str, config: Config) -> str:
    """Execute claude -p with message, return response text.

    Args:
        message: User message to send to claude.
        config: Bot configuration.

    Returns:
        Claude's response text.

    Raises:
        TimeoutError: If claude takes longer than config.claude_timeout.
        RuntimeError: If claude exits with non-zero code or empty response.
    """
    memory = _build_memory_context()
    system = config.system_prompt
    if memory:
        system = f"{system}\n\n{memory}"

    cmd = [
        config.claude_bin,
        "-p", message,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", "opus",
        "--append-system-prompt", system,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=config.claude_cwd,
        env=_claude_env(),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=config.claude_timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise TimeoutError(
            f"Claude did not respond within {config.claude_timeout}s"
        )

    if stderr:
        logger.warning("claude stderr: %s", stderr.decode(errors="replace").strip())

    if process.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"claude exited with code {process.returncode}: {err}")

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        raise RuntimeError("claude returned empty output")

    # Parse JSON output, extract result field
    try:
        data = json.loads(raw)
        result = data.get("result", "")
        if not result:
            raise RuntimeError("claude returned empty result field")
        return result
    except json.JSONDecodeError:
        # Fallback: return raw stdout if JSON parsing fails
        logger.warning("Failed to parse claude JSON output, using raw stdout")
        return raw


async def stream_claude(message: str, config: Config) -> AsyncGenerator[str | None, None]:
    """Yield text deltas from claude stream-json output. None = done.

    Args:
        message: User message to send to claude.
        config: Bot configuration.

    Yields:
        str chunks of text as they arrive, then None when complete.

    Raises:
        TimeoutError: If no output received for config.claude_timeout seconds.
        RuntimeError: If claude exits with non-zero code.
    """
    memory = _build_memory_context()
    system = config.system_prompt
    if memory:
        system = f"{system}\n\n{memory}"

    cmd = [
        config.claude_bin,
        "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--dangerously-skip-permissions",
        "--model", "opus",
        "--append-system-prompt", system,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=config.claude_cwd,
        env=_claude_env(),
    )

    got_text = False
    try:
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=config.claude_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError(
                    f"Claude did not respond within {config.claude_timeout}s"
                )

            if not line:
                break

            raw = line.decode(errors="replace").strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # Extract text deltas from stream events
            if msg_type == "stream_event":
                event = data.get("event", {})
                if (
                    event.get("type") == "content_block_delta"
                    and event.get("delta", {}).get("type") == "text_delta"
                ):
                    text = event["delta"].get("text", "")
                    if text:
                        got_text = True
                        yield text

            # Final result — signal completion
            elif msg_type == "result":
                yield None
                return

    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    # Process ended without result event
    if process.returncode != 0:
        stderr = await process.stderr.read()
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"claude exited with code {process.returncode}: {err}")

    if not got_text:
        raise RuntimeError("claude stream ended with no text output")

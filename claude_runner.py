"""Async wrapper for claude CLI subprocess execution."""

import asyncio
import json
import logging
import os

from config import Config

logger = logging.getLogger(__name__)


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
    cmd = [
        config.claude_bin,
        "-p", message,
        "--output-format", "json",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=config.claude_cwd,
        env={**os.environ, "NO_COLOR": "1"},
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

#!/usr/bin/env python3
"""Save Claude Code messages to SQLite database.

Handles both UserPromptSubmit and Stop events.
On Stop: saves the assistant response AND generates a session summary.
"""

import json
import os
import re
import sys
from pathlib import Path

# Add project root to path so we can import db module
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, PROJECT_ROOT)
from db import save_message, save_summary, save_fact, save_task


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", "unknown")
    event = data.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        content = data.get("prompt", "")
        if content:
            save_message("user", content, source="claude-code", session_id=session_id)
            # Extract persistent facts from user messages
            _extract_facts(content)

    elif event == "Stop":
        response = data.get("stop_hook_active_response", "")
        transcript_path = data.get("transcript_path", "")

        if response:
            save_message("assistant", response, source="claude-code", session_id=session_id)

        # Generate session summary from transcript
        if transcript_path and os.path.exists(transcript_path):
            if not response:
                _save_from_transcript(session_id, transcript_path)
            _generate_session_summary(session_id, transcript_path)

    json.dump({"continue": True}, sys.stdout)


def _extract_facts(user_message):
    """Detect 'remember that...' patterns and persist as facts."""
    patterns = [
        r"(?:retiens?|remember|note|rappelle[- ]toi|n'oublie pas|oublie pas)\s+(?:que\s+)?(.+)",
        r"(?:toujours|always)\s+(.+)",
        r"(?:jamais|never)\s+(.+)",
        r"(?:je pr[eé]f[eè]re?|i prefer)\s+(.+)",
    ]
    lower = user_message.lower().strip()
    for pattern in patterns:
        match = re.search(pattern, lower, re.IGNORECASE)
        if match:
            fact = match.group(1).strip().rstrip(".")
            if len(fact) > 10:  # Skip very short matches
                save_fact(fact, category="preference", source="user")
                break


def _generate_session_summary(session_id, transcript_path):
    """Extract a structured summary from the session transcript.

    No LLM call — purely structural extraction from the JSONL transcript.
    """
    try:
        user_topics = []
        files_modified = set()
        decisions = []
        tool_uses = []

        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")

                # Extract user messages as topic indicators
                if entry_type == "human":
                    msg = entry.get("message", {})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, str) and len(content) > 15:
                        # Take first 100 chars as topic indicator
                        user_topics.append(content[:100].strip())

                # Extract tool usage for files modified
                elif entry_type == "assistant":
                    msg = entry.get("message", {})
                    content_parts = msg.get("content", []) if isinstance(msg, dict) else []
                    for part in content_parts:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool_use":
                            tool_name = part.get("name", "")
                            tool_input = part.get("input", {})
                            tool_uses.append(tool_name)

                            # Track file modifications
                            if tool_name in ("Edit", "Write"):
                                fp = tool_input.get("file_path", "")
                                if fp:
                                    files_modified.add(fp)

                            # Track Bash commands that modify files
                            elif tool_name == "Bash":
                                cmd = tool_input.get("command", "")
                                if any(kw in cmd for kw in ["git commit", "rm ", "mv ", "cp "]):
                                    decisions.append(f"cmd: {cmd[:80]}")

                        # Look for decision-like content in assistant text
                        elif part.get("type") == "text":
                            text = part.get("text", "")
                            for marker in ["decision:", "decided:", "chosen:", "choisi:", "décidé:"]:
                                if marker in text.lower():
                                    idx = text.lower().index(marker)
                                    snippet = text[idx:idx + 150].strip()
                                    decisions.append(snippet)

        # Build summary
        if not user_topics and not files_modified:
            return  # Empty session, skip

        summary_parts = []
        if user_topics:
            summary_parts.append("Topics: " + " | ".join(user_topics[:5]))
        if tool_uses:
            from collections import Counter
            counts = Counter(tool_uses)
            top = ", ".join(f"{k}({v})" for k, v in counts.most_common(5))
            summary_parts.append(f"Tools: {top}")
        if files_modified:
            # Shorten paths for readability
            short = [fp.split("/")[-1] for fp in sorted(files_modified)]
            summary_parts.append(f"Files: {', '.join(short[:8])}")

        summary = "\n  ".join(summary_parts) if summary_parts else "Session with no extractable topics"

        save_summary(
            session_id=session_id,
            summary=summary,
            decisions="\n".join(decisions[:5]) if decisions else "",
            files_modified=", ".join(sorted(files_modified)) if files_modified else "",
        )

    except (IOError, OSError):
        pass


def _save_from_transcript(session_id, transcript_path):
    """Fallback: extract the LAST assistant message from JSONL transcript."""
    try:
        last_text = None
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        msg = entry.get("message", {})
                        content_parts = msg.get("content", []) if isinstance(msg, dict) else []
                        text_parts = []
                        for part in content_parts:
                            if isinstance(part, str):
                                text_parts.append(part)
                            elif isinstance(part, dict) and part.get("type") == "text":
                                text_parts.append(part.get("text", ""))
                        if text_parts:
                            last_text = "\n".join(text_parts)
                except json.JSONDecodeError:
                    continue
        if last_text:
            save_message("assistant", last_text, source="claude-code", session_id=session_id)
    except (IOError, OSError):
        pass


if __name__ == "__main__":
    main()

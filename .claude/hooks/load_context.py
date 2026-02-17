#!/usr/bin/env python3
"""Load recent messages from database into Claude's context on session start.

Runs as a SessionStart hook — loads once per session, not on every prompt.

Applies several optimizations:
1. Cross-source dedup: when CC and TG have same assistant response, keep TG only
2. Short message filtering: skip low-value messages (< 15 chars)
3. Compact assistant summaries: truncate long assistant messages, keep user messages full
4. Smart truncation: cut at last section boundary instead of mid-sentence
5. Temporal coverage: use token budget to cover more time, not just last N messages
6. Session separators: group messages by time gaps
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "database.db")
TOKEN_BUDGET = 4000  # ~4000 tokens worth of context
CHARS_PER_TOKEN = 4  # rough estimate
CHAR_BUDGET = TOKEN_BUDGET * CHARS_PER_TOKEN
FETCH_LIMIT = 120  # fetch more raw messages to have wider temporal window
SHORT_MSG_THRESHOLD = 15  # chars — skip user messages shorter than this
ASSISTANT_MAX_LEN = 800  # compact assistant messages more aggressively
USER_MAX_LEN = 2000  # keep user messages longer (they contain decisions)
SESSION_GAP_MINUTES = 30  # gap between messages to insert session separator


def get_recent_messages():
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            """SELECT role, content, metadata, source, content_hash, id
               FROM messages ORDER BY id DESC LIMIT ?""",
            (FETCH_LIMIT,),
        )
        rows = cursor.fetchall()
        conn.close()
        return list(reversed(rows))
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def dedup_consecutive(rows):
    """Remove consecutive messages with the same content hash."""
    result = []
    prev_hash = None
    for row in rows:
        c_hash = row[4]
        if c_hash and c_hash == prev_hash:
            continue
        prev_hash = c_hash
        result.append(row)
    return result


def dedup_cross_source(rows):
    """When CC and TG have similar assistant messages close together, keep TG only.

    Pattern: CC saves the raw response, TG saves the formatted version sent to user.
    The TG version is more relevant (it's what the user actually saw).
    """
    to_remove = set()
    for i, row in enumerate(rows):
        role, content, _meta, source, _hash, msg_id = row
        if role != "assistant" or source != "claude-code":
            continue
        # Look for a nearby TG assistant message (within 3 positions)
        for j in range(max(0, i - 3), min(len(rows), i + 4)):
            if j == i:
                continue
            other = rows[j]
            if other[0] == "assistant" and other[3] == "telegram":
                # CC message near a TG message — CC is likely the intermediate/raw version
                to_remove.add(i)
                break
    return [row for i, row in enumerate(rows) if i not in to_remove]


def filter_short_messages(rows):
    """Remove short user messages with low contextual value."""
    return [
        row for row in rows
        if not (row[0] == "user" and len(row[1].strip()) < SHORT_MSG_THRESHOLD)
    ]


def smart_truncate(content, max_len):
    """Truncate at a section boundary (##, ---, blank line) instead of mid-text."""
    if len(content) <= max_len:
        return content

    # Try to find a good break point near the limit
    search_zone = content[max_len // 2:max_len]

    # Look for section boundaries (in reverse order of preference)
    for marker in ["\n## ", "\n---", "\n\n"]:
        pos = search_zone.rfind(marker)
        if pos != -1:
            cut = max_len // 2 + pos
            return content[:cut] + "\n[...]"

    # Fallback: cut at last sentence end
    last_period = content[:max_len].rfind(". ")
    if last_period > max_len // 2:
        return content[:last_period + 1] + " [...]"

    return content[:max_len] + "..."


def parse_timestamp(metadata):
    """Extract datetime from metadata."""
    if not metadata:
        return None
    try:
        meta = json.loads(metadata)
        ts = meta.get("created_at", "")
        if ts:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def format_source(source):
    labels = {
        "claude-code": "CC",
        "telegram": "TG",
        "web": "WEB",
    }
    return labels.get(source, source or "CC")


def build_context(rows):
    """Build context string with session separators and token budget.

    Strategy: fill from most recent first to guarantee recent context,
    then prepend older messages until budget is exhausted.
    """
    # Build formatted entries from most recent to oldest
    entries = []
    total_chars = 0

    for row in reversed(rows):
        role, content, metadata, source, _hash, _id = row
        meta = json.loads(metadata) if metadata else {}
        created = meta.get("created_at", "")[:19]
        src = format_source(source)
        prefix = "Oneup" if role == "user" else os.environ.get("ASSISTANT_NAME", "Nova")

        # Fix #3 + #4: Smart truncation with role-based limits
        max_len = USER_MAX_LEN if role == "user" else ASSISTANT_MAX_LEN
        content = smart_truncate(content, max_len)

        line = f"[{created}][{src}] {prefix}: {content}"

        # Fix #5: Token budget — stop adding when budget exhausted
        if total_chars + len(line) > CHAR_BUDGET:
            break
        total_chars += len(line)
        entries.append((line, metadata))

    # Reverse back to chronological order
    entries.reverse()

    # Fix #6: Insert session separators based on time gaps
    assistant_name = os.environ.get("ASSISTANT_NAME", "Nova")
    lines = [
        "[Conversation history from previous sessions]",
        "",
        "",
        "",
        f"IMPORTANT: This is a CONTINUING conversation. Do NOT greet the user again "
        f"(no 'Salut', 'Bonjour', etc.) — you already know each other. "
        f"Do NOT re-examine code or state you already checked in recent history. "
        f"Pick up naturally where the conversation left off.",
        "",
    ]
    prev_time = None
    for line, metadata in entries:
        curr_time = parse_timestamp(metadata)
        if prev_time and curr_time:
            gap = (curr_time - prev_time).total_seconds() / 60
            if gap > SESSION_GAP_MINUTES:
                lines.append("--- new session ---")
        prev_time = curr_time
        lines.append(line)

    lines.append("[End of history]")
    return "\n\n".join(lines)


def main():
    json.load(sys.stdin)  # consume stdin (required by hook protocol)

    rows = get_recent_messages()

    if rows:
        # Fix #1: Dedup consecutive same-hash messages
        rows = dedup_consecutive(rows)
        # Fix #1b: Dedup cross-source CC+TG assistant messages
        rows = dedup_cross_source(rows)
        # Fix #2: Filter short user messages
        rows = filter_short_messages(rows)

        context = build_context(rows)
    else:
        context = ""

    output = {"continue": True}
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }

    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()

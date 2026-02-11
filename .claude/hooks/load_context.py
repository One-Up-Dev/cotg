#!/usr/bin/env python3
"""Load recent messages from database into Claude's context on session start.

Runs as a SessionStart hook — loads once per session, not on every prompt.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "database.db")
MESSAGE_LIMIT = 30
CONTENT_MAX_LEN = 800


def get_recent_messages(limit=MESSAGE_LIMIT):
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            """SELECT role, content, metadata, source
               FROM messages ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        rows = cursor.fetchall()
        conn.close()
        return list(reversed(rows))
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def format_source(source):
    labels = {
        "claude-code": "CC",
        "telegram": "TG",
        "web": "WEB",
    }
    return labels.get(source, source or "CC")


def main():
    data = json.load(sys.stdin)

    messages = get_recent_messages()

    if messages:
        lines = ["[Conversation history from previous sessions]"]
        for role, content, metadata, source in messages:
            meta = json.loads(metadata) if metadata else {}
            created = meta.get("created_at", "")[:19]  # trim microseconds
            src = format_source(source)
            prefix = "Oneup" if role == "user" else "Claude"

            if len(content) > CONTENT_MAX_LEN:
                content = content[:CONTENT_MAX_LEN] + "..."

            lines.append(f"[{created}][{src}] {prefix}: {content}")

        lines.append("[End of history]")
        context = "\n\n".join(lines)
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

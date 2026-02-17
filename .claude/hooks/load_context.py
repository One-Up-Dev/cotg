#!/usr/bin/env python3
"""Load multi-layer memory into Claude's context on session start.

Runs as a SessionStart hook — loads once per session, not on every prompt.

Memory layers injected:
1. Long-term memory: persistent facts and preferences (table: facts)
2. Medium-term memory: recent session summaries (table: summaries)
3. Active tasks: pending/in-progress tasks (table: tasks)
4. Short-term memory: recent conversation messages (table: messages)

Optimizations on message history:
- Cross-source dedup: when CC and TG have same assistant response, keep TG only
- Short message filtering: skip low-value messages (< 15 chars)
- Compact assistant summaries: truncate long assistant messages, keep user messages full
- Smart truncation: cut at last section boundary instead of mid-sentence
- Token budget: cover more time, not just last N messages
- Session separators: group messages by time gaps
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "database.db")

# Token budgets per memory layer
TOTAL_TOKEN_BUDGET = 6000
FACTS_TOKEN_BUDGET = 500       # ~2000 chars — persistent facts
SUMMARIES_TOKEN_BUDGET = 1000  # ~4000 chars — session summaries
TASKS_TOKEN_BUDGET = 500       # ~2000 chars — active tasks
# Remaining goes to conversation history
CHARS_PER_TOKEN = 4

FETCH_LIMIT = 120
SHORT_MSG_THRESHOLD = 15
ASSISTANT_MAX_LEN = 800
USER_MAX_LEN = 2000
SESSION_GAP_MINUTES = 30


# ── Message history (short-term) ─────────────────────────────────────

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
    """When CC and TG have similar assistant messages close together, keep TG only."""
    to_remove = set()
    for i, row in enumerate(rows):
        role, content, _meta, source, _hash, msg_id = row
        if role != "assistant" or source != "claude-code":
            continue
        for j in range(max(0, i - 3), min(len(rows), i + 4)):
            if j == i:
                continue
            other = rows[j]
            if other[0] == "assistant" and other[3] == "telegram":
                to_remove.add(i)
                break
    return [row for i, row in enumerate(rows) if i not in to_remove]


def filter_short_messages(rows):
    return [
        row for row in rows
        if not (row[0] == "user" and len(row[1].strip()) < SHORT_MSG_THRESHOLD)
    ]


def smart_truncate(content, max_len):
    if len(content) <= max_len:
        return content
    search_zone = content[max_len // 2:max_len]
    for marker in ["\n## ", "\n---", "\n\n"]:
        pos = search_zone.rfind(marker)
        if pos != -1:
            cut = max_len // 2 + pos
            return content[:cut] + "\n[...]"
    last_period = content[:max_len].rfind(". ")
    if last_period > max_len // 2:
        return content[:last_period + 1] + " [...]"
    return content[:max_len] + "..."


def parse_timestamp(metadata):
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
    labels = {"claude-code": "CC", "telegram": "TG", "web": "WEB"}
    return labels.get(source, source or "CC")


# ── Memory layers (medium/long-term) ─────────────────────────────────

def get_facts():
    """Load persistent facts from DB."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        # Check if table exists
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='facts'"
        ).fetchone()
        if not table:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT category, content FROM facts WHERE active = 1 ORDER BY category, id"
        ).fetchall()
        conn.close()
        return rows
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def get_summaries(limit=5):
    """Load recent session summaries from DB."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'"
        ).fetchone()
        if not table:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT summary, decisions, files_modified, created_at "
            "FROM summaries ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return list(reversed(rows))
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def get_tasks():
    """Load active tasks from DB."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_tasks'"
        ).fetchone()
        if not table:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT title, status, context, updated_at FROM memory_tasks "
            "WHERE status != 'done' ORDER BY id"
        ).fetchall()
        conn.close()
        return rows
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


# ── Context builders ──────────────────────────────────────────────────

def build_facts_context(facts):
    """Format facts into context block."""
    if not facts:
        return ""
    lines = ["[Long-term memory — persistent facts]"]
    current_cat = None
    for category, content in facts:
        if category != current_cat:
            current_cat = category
            lines.append(f"  [{category}]")
        lines.append(f"  - {content}")
    return "\n".join(lines)


def build_summaries_context(summaries):
    """Format session summaries into context block."""
    if not summaries:
        return ""
    lines = ["[Medium-term memory — recent session summaries]"]
    for summary, decisions, files_modified, created_at in summaries:
        date = created_at[:10] if created_at else "?"
        lines.append(f"  [{date}] {summary}")
        if files_modified:
            lines.append(f"    Files: {files_modified}")
        if decisions:
            lines.append(f"    Decisions: {decisions[:200]}")
    return "\n".join(lines)


def build_tasks_context(tasks):
    """Format active tasks into context block."""
    if not tasks:
        return ""
    lines = ["[Active tasks]"]
    for title, status, context, updated_at in tasks:
        icon = {"pending": "[ ]", "in_progress": "[>]", "blocked": "[!]"}.get(status, "[ ]")
        line = f"  {icon} {title}"
        if context:
            line += f" — {context[:100]}"
        lines.append(line)
    return "\n".join(lines)


def build_messages_context(rows, char_budget):
    """Build conversation history within the given char budget."""
    entries = []
    total_chars = 0

    for row in reversed(rows):
        role, content, metadata, source, _hash, _id = row
        meta = json.loads(metadata) if metadata else {}
        created = meta.get("created_at", "")[:19]
        src = format_source(source)
        prefix = "Oneup" if role == "user" else os.environ.get("ASSISTANT_NAME", "Nova")

        max_len = USER_MAX_LEN if role == "user" else ASSISTANT_MAX_LEN
        content = smart_truncate(content, max_len)
        line = f"[{created}][{src}] {prefix}: {content}"

        if total_chars + len(line) > char_budget:
            break
        total_chars += len(line)
        entries.append((line, metadata))

    entries.reverse()

    lines = []
    prev_time = None
    for line, metadata in entries:
        curr_time = parse_timestamp(metadata)
        if prev_time and curr_time:
            gap = (curr_time - prev_time).total_seconds() / 60
            if gap > SESSION_GAP_MINUTES:
                lines.append("--- new session ---")
        prev_time = curr_time
        lines.append(line)

    return "\n\n".join(lines)


# ── Main assembly ─────────────────────────────────────────────────────

def build_full_context(rows, facts, summaries, tasks):
    """Assemble all memory layers into a single context string."""
    sections = []

    # Layer 1: Long-term facts
    facts_ctx = build_facts_context(facts)
    if facts_ctx:
        sections.append(facts_ctx)

    # Layer 2: Session summaries (medium-term)
    summaries_ctx = build_summaries_context(summaries)
    if summaries_ctx:
        sections.append(summaries_ctx)

    # Layer 3: Active tasks
    tasks_ctx = build_tasks_context(tasks)
    if tasks_ctx:
        sections.append(tasks_ctx)

    # Layer 4: Conversation history (short-term)
    # Calculate remaining budget for messages
    used_chars = sum(len(s) for s in sections)
    messages_budget = (TOTAL_TOKEN_BUDGET * CHARS_PER_TOKEN) - used_chars
    messages_budget = max(messages_budget, 8000)  # minimum 8000 chars for messages

    if rows:
        messages_ctx = build_messages_context(rows, messages_budget)
        if messages_ctx:
            sections.append(messages_ctx)

    if not sections:
        return ""

    # Wrap with instructions
    header = (
        "[Conversation history from previous sessions]\n\n\n\n"
        "IMPORTANT: This is a CONTINUING conversation. Do NOT greet the user again "
        "(no 'Salut', 'Bonjour', etc.) — you already know each other. "
        "Do NOT re-examine code or state you already checked in recent history. "
        "Pick up naturally where the conversation left off.\n"
    )
    footer = "\n[End of history]"

    return header + "\n\n".join(sections) + footer


def main():
    json.load(sys.stdin)  # consume stdin (required by hook protocol)

    # Load all memory layers
    rows = get_recent_messages()
    facts = get_facts()
    summaries = get_summaries(limit=5)
    tasks = get_tasks()

    if rows:
        rows = dedup_consecutive(rows)
        rows = dedup_cross_source(rows)
        rows = filter_short_messages(rows)

    context = build_full_context(rows, facts, summaries, tasks)

    output = {"continue": True}
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }

    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()

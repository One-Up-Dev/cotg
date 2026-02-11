"""Shared SQLite database module for conversation persistence."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent / "database.db")
DEDUP_WINDOW_SECONDS = 5
MAX_MESSAGES = 5000


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)
    return conn


def _init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            source TEXT DEFAULT 'claude-code'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages (source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role ON messages (role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id_desc ON messages (id DESC)")
    conn.commit()


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(conn, role, c_hash, source, now_iso):
    """Check if a message with the same hash was saved within the dedup window."""
    row = conn.execute(
        """SELECT metadata FROM messages
           WHERE role = ? AND source = ?
           ORDER BY id DESC LIMIT 1""",
        (role, source),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        meta = json.loads(row[0])
        if meta.get("content_hash", "") != c_hash:
            return False
        last_time = meta.get("created_at", "")
        if not last_time:
            return False
        last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return abs((now_dt - last_dt).total_seconds()) < DEDUP_WINDOW_SECONDS
    except (json.JSONDecodeError, ValueError):
        return False


def save_message(role, content, source="claude-code", session_id=""):
    """Save a message to the database with deduplication."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        c_hash = _content_hash(content)

        if _is_duplicate(conn, role, c_hash, source, now):
            conn.close()
            return

        metadata = json.dumps({
            "session_id": session_id,
            "created_at": now,
            "content_hash": c_hash,
        })
        conn.execute(
            "INSERT INTO messages (role, content, metadata, source) VALUES (?, ?, ?, ?)",
            (role, content, metadata, source),
        )
        conn.commit()
        _maybe_rotate(conn)
        conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to save message: %s", e)


def _maybe_rotate(conn):
    """Keep only the last MAX_MESSAGES rows."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if count > MAX_MESSAGES:
            conn.execute(
                """DELETE FROM messages WHERE id NOT IN (
                    SELECT id FROM messages ORDER BY id DESC LIMIT ?
                )""",
                (MAX_MESSAGES,),
            )
            conn.commit()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        pass

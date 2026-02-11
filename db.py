"""Shared SQLite database module for conversation persistence."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent / "database.db")
DEDUP_WINDOW_SECONDS = 60
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
            source TEXT DEFAULT 'claude-code',
            content_hash TEXT
        )
    """)
    # Add content_hash column if missing (migration)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages (source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role ON messages (role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id_desc ON messages (id DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages (role, content_hash)"
    )
    conn.commit()


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(conn, role, c_hash):
    """Check if a message with the same hash exists in the dedup window."""
    row = conn.execute(
        """SELECT metadata FROM messages
           WHERE role = ? AND content_hash = ?
           ORDER BY id DESC LIMIT 1""",
        (role, c_hash),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        meta = json.loads(row[0])
        last_time = meta.get("created_at", "")
        if not last_time:
            return False
        last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        return abs((now_dt - last_dt).total_seconds()) < DEDUP_WINDOW_SECONDS
    except (json.JSONDecodeError, ValueError):
        return False


def save_message(role, content, source="claude-code", session_id=""):
    """Save a message to the database with deduplication."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        c_hash = _content_hash(content)

        if _is_duplicate(conn, role, c_hash):
            conn.close()
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        metadata = json.dumps({
            "session_id": session_id,
            "created_at": now,
            "content_hash": c_hash,
        })
        conn.execute(
            "INSERT INTO messages (role, content, metadata, source, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, content, metadata, source, c_hash),
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

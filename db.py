"""Shared SQLite database module for conversation persistence."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent / "database.db")
MAX_MESSAGES = 5000
_db_initialized: set[str] = set()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if DB_PATH not in _db_initialized:
        _init_db(conn)
        _db_initialized.add(DB_PATH)
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
    """Check if the last message with the same role has the same content hash."""
    row = conn.execute(
        "SELECT content_hash FROM messages WHERE role = ? ORDER BY id DESC LIMIT 1",
        (role,),
    ).fetchone()
    if not row:
        return False
    return row["content_hash"] == c_hash


def save_message(role, content, source="claude-code", session_id=""):
    """Save a message to the database with deduplication."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        try:
            c_hash = _content_hash(content)

            if _is_duplicate(conn, role, c_hash):
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
        finally:
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


# ── Memory tables ────────────────────────────────────────────────────

MAX_SUMMARIES = 50  # keep last 50 session summaries (~25 days at 2/day)
MAX_TASKS = 200


def _init_memory_tables(conn):
    """Create memory tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            summary TEXT NOT NULL,
            decisions TEXT,
            files_modified TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            source TEXT DEFAULT 'user',
            created_at TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            context TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_active ON facts (active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_category ON facts (category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_tasks_status ON memory_tasks (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_created ON summaries (created_at DESC)")
    conn.commit()


def save_summary(session_id, summary, decisions="", files_modified=""):
    """Save a session summary to the database."""
    if not summary or not summary.strip():
        return
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "INSERT INTO summaries (session_id, summary, decisions, files_modified, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, summary.strip(), decisions, files_modified, now),
            )
            conn.commit()
            _rotate_summaries(conn)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to save summary: %s", e)


def _rotate_summaries(conn):
    """Keep only the last MAX_SUMMARIES rows."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        if count > MAX_SUMMARIES:
            conn.execute(
                """DELETE FROM summaries WHERE id NOT IN (
                    SELECT id FROM summaries ORDER BY id DESC LIMIT ?
                )""",
                (MAX_SUMMARIES,),
            )
            conn.commit()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        pass


def get_recent_summaries(limit=5):
    """Get the N most recent session summaries."""
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            rows = conn.execute(
                "SELECT summary, decisions, files_modified, created_at "
                "FROM summaries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return list(reversed(rows))
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def save_fact(content, category="general", source="user"):
    """Save a persistent fact to the database."""
    if not content or not content.strip():
        return
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "INSERT INTO facts (category, content, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (category, content.strip(), source, now),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to save fact: %s", e)


def get_active_facts():
    """Get all active facts."""
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            rows = conn.execute(
                "SELECT category, content FROM facts WHERE active = 1 ORDER BY category, id",
            ).fetchall()
            return rows
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def save_task(title, status="pending", context=""):
    """Save or update a task."""
    if not title or not title.strip():
        return
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            # Check if task with same title exists
            existing = conn.execute(
                "SELECT id FROM memory_tasks WHERE title = ?", (title.strip(),)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memory_tasks SET status = ?, context = ?, updated_at = ? WHERE id = ?",
                    (status, context, now, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO memory_tasks (title, status, context, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (title.strip(), status, context, now, now),
                )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to save task: %s", e)


def get_active_tasks():
    """Get all non-completed tasks."""
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            rows = conn.execute(
                "SELECT title, status, context, updated_at FROM memory_tasks "
                "WHERE status != 'done' ORDER BY id",
            ).fetchall()
            return rows
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return []


def complete_task(title):
    """Mark a task as done."""
    try:
        conn = get_connection()
        try:
            _init_memory_tables(conn)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "UPDATE memory_tasks SET status = 'done', updated_at = ? WHERE title = ?",
                (now, title.strip()),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        logger.warning("Failed to complete task: %s", e)

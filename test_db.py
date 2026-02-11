"""Tests for db.py deduplication logic."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# Import the module under test
sys.path.insert(0, os.path.dirname(__file__))
import db


class TestDedup(unittest.TestCase):
    """Test deduplication in an isolated in-memory DB."""

    def setUp(self):
        """Patch DB_PATH to use a temp file for isolation."""
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.patcher = patch.object(db, "DB_PATH", self.tmp.name)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.tmp.name)

    def _count(self, content=None):
        conn = sqlite3.connect(self.tmp.name)
        if content:
            n = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE content = ?", (content,)
            ).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return n

    def _get_rows(self, content):
        conn = sqlite3.connect(self.tmp.name)
        rows = conn.execute(
            "SELECT id, source, content_hash FROM messages WHERE content = ?",
            (content,),
        ).fetchall()
        conn.close()
        return rows

    # --- Basic save ---

    def test_save_message_basic(self):
        db.save_message("user", "hello", source="telegram")
        self.assertEqual(self._count("hello"), 1)

    def test_save_fills_content_hash_column(self):
        db.save_message("user", "hello", source="telegram")
        rows = self._get_rows("hello")
        expected = hashlib.sha256(b"hello").hexdigest()[:16]
        self.assertEqual(rows[0][2], expected)

    def test_save_empty_content_skipped(self):
        # Save a real message first so the table exists
        db.save_message("user", "real", source="telegram")
        db.save_message("user", "", source="telegram")
        db.save_message("user", "   ", source="telegram")
        self.assertEqual(self._count(), 1)  # only "real"

    # --- Same source dedup ---

    def test_dedup_same_source(self):
        db.save_message("user", "dup", source="telegram")
        db.save_message("user", "dup", source="telegram")
        self.assertEqual(self._count("dup"), 1)

    # --- Cross-source dedup (the main bug) ---

    def test_dedup_cross_source_telegram_then_claude(self):
        """bot.py saves telegram, then hook saves claude-code."""
        db.save_message("user", "cross", source="telegram")
        db.save_message("user", "cross", source="claude-code")
        self.assertEqual(self._count("cross"), 1)

    def test_dedup_cross_source_claude_then_telegram(self):
        """Hook saves claude-code, then bot.py saves telegram."""
        db.save_message("user", "cross2", source="claude-code")
        db.save_message("user", "cross2", source="telegram")
        self.assertEqual(self._count("cross2"), 1)

    # --- Dedup with NULL content_hash column (legacy rows) ---

    def test_dedup_when_existing_row_has_null_hash_column(self):
        """Simulate old code: hash in metadata JSON but NULL in column."""
        conn = db.get_connection()
        c_hash = db._content_hash("legacy")
        metadata = json.dumps({
            "session_id": "",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "content_hash": c_hash,
        })
        # Insert WITHOUT content_hash column (old code behavior)
        conn.execute(
            "INSERT INTO messages (role, content, metadata, source) VALUES (?, ?, ?, ?)",
            ("user", "legacy", metadata, "telegram"),
        )
        conn.commit()
        conn.close()

        # Now try to save same message via new code
        db.save_message("user", "legacy", source="claude-code")
        self.assertEqual(self._count("legacy"), 1)

    # --- Dedup window ---

    def test_same_content_after_window_is_allowed(self):
        """Messages beyond the dedup window should be saved."""
        with patch.object(db, "DEDUP_WINDOW_SECONDS", 0):
            db.save_message("user", "again", source="telegram")
            time.sleep(0.01)
            db.save_message("user", "again", source="telegram")
            self.assertEqual(self._count("again"), 2)

    # --- Different roles are not deduped ---

    def test_different_roles_not_deduped(self):
        db.save_message("user", "same", source="telegram")
        db.save_message("assistant", "same", source="telegram")
        self.assertEqual(self._count("same"), 2)

    # --- Cross-process dedup (simulates hook as subprocess) ---

    def test_dedup_cross_process(self):
        """Simulate bot.py saving, then hook saving in a separate process."""
        db.save_message("user", "crossproc", source="telegram")

        # Run save_message in a subprocess (like claude hook would)
        subprocess.run(
            [
                sys.executable, "-c",
                f"""
import sys; sys.path.insert(0, {os.path.dirname(__file__)!r})
from unittest.mock import patch
import db
with patch.object(db, "DB_PATH", {self.tmp.name!r}):
    db.save_message("user", "crossproc", source="claude-code")
""",
            ],
            check=True,
        )
        self.assertEqual(self._count("crossproc"), 1)


if __name__ == "__main__":
    unittest.main()

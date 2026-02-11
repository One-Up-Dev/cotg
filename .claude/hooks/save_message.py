#!/usr/bin/env python3
"""Save Claude Code messages to SQLite database.

Handles both UserPromptSubmit and Stop events.
"""

import json
import os
import sys
from pathlib import Path

# Add project root to path so we can import db module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from db import save_message


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", "unknown")
    event = data.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        content = data.get("prompt", "")
        if content:
            save_message("user", content, source="claude-code", session_id=session_id)

    elif event == "Stop":
        response = data.get("stop_hook_active_response", "")
        if response:
            save_message("assistant", response, source="claude-code", session_id=session_id)
        else:
            transcript_path = data.get("transcript_path", "")
            if transcript_path and os.path.exists(transcript_path):
                _save_from_transcript(session_id, transcript_path)

    json.dump({"continue": True}, sys.stdout)


def _save_from_transcript(session_id, transcript_path):
    """Fallback: extract assistant messages from JSONL transcript."""
    try:
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
                            full_text = "\n".join(text_parts)
                            save_message("assistant", full_text, source="claude-code", session_id=session_id)
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        pass


if __name__ == "__main__":
    main()

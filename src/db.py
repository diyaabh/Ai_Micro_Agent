import os
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime
import json

from .config import DATABASE_URL

# Example: DATABASE_URL = "sqlite:///ai_agent.db"
DB_FILE = DATABASE_URL.replace("sqlite:///", "")


def init_db():
    schema_file = Path(__file__).parent / "schema.sql"  # <-- define schema_file path
    if not Path(DB_FILE).exists():
        conn = sqlite3.connect(DB_FILE)
        with closing(conn):
            cur = conn.cursor()
            with open(schema_file, "r", encoding="utf-8") as f:
                cur.executescript(f.read())
            conn.commit()


def get_conn():
    """Get a new DB connection."""
    print(f"📂 Connecting to DB file: {DB_FILE}")
    return sqlite3.connect(DB_FILE)


def create_user(name: str, chat_id: str, timezone: str = "Asia/Kolkata"):
    """Create a new user in the user table."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user (name, chat_id, timezone) VALUES (?, ?, ?)",
        (name, chat_id, timezone),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def create_task(
    user_id: int,
    task_type: str,
    plan: dict,
    schedule_rule: str = "*",
    enabled: int = 1,
):
    """Create a new task linked to a user."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO task (user_id, type, params_json, schedule_rule, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, task_type, json.dumps(plan), schedule_rule, enabled),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def list_tasks():
    """Return all tasks (helper; not heavily used right now)."""
    conn = get_conn()
    cur = conn.cursor()
    # Column is 'type' in the table, not 'task_type'
    cur.execute(
        "SELECT id, type, params_json, schedule_rule, enabled FROM task"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# Auto initialize database if missing
init_db()


# -------------------- Notes helpers --------------------
def create_note(user_chat_id: str, text: str) -> int:
    """Create a new note for this Telegram chat_id. Returns the note's id."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO note (user_chat_id, text, created_at)
        VALUES (?, ?, ?)
        """,
        (user_chat_id, text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    note_id = cur.lastrowid
    conn.close()
    return note_id


def list_notes(user_chat_id: str):
    """
    Return a list of (id, text, created_at, pinned) for notes of this chat_id.

    Pinned notes come first, then others by newest created_at.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, text, created_at, pinned
        FROM note
        WHERE user_chat_id = ?
        ORDER BY pinned DESC, created_at DESC
        """,
        (user_chat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_note(user_chat_id: str, note_id: int) -> bool:
    """Delete a note belonging to this chat_id. Returns True if deleted."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM note
        WHERE user_chat_id = ? AND id = ?
        """,
        (user_chat_id, note_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def pin_note(user_chat_id: str, note_id: int) -> bool:
    """Mark a note as pinned (pinned = 1). Returns True if updated."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE note
        SET pinned = 1
        WHERE user_chat_id = ? AND id = ?
        """,
        (user_chat_id, note_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def unpin_note(user_chat_id: str, note_id: int) -> bool:
    """Remove pinned mark from a note (pinned = 0). Returns True if updated."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE note
        SET pinned = 0
        WHERE user_chat_id = ? AND id = ?
        """,
        (user_chat_id, note_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated

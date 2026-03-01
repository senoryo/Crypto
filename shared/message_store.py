"""
SQLite-backed message store for inter-component message tracing.

Each component's log_recv/log_send calls feed messages into a shared
SQLite database (WAL mode) so the troubleshoot AI can inspect actual
message flows rather than just raw log tails.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependency at module level
_db_path = None
_insert_count = 0
_insert_lock = threading.Lock()
_CLEANUP_EVERY = 100
_MAX_RAW_LEN = 2000
_db_initialized = False
_persistent_conn = None


def _get_db_path():
    global _db_path
    if _db_path is None:
        from shared.config import MESSAGE_DB_FILE
        _db_path = MESSAGE_DB_FILE
    return _db_path


def init_db():
    """Create the messages table and index if they don't exist. Enable WAL mode."""
    path = _get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                component TEXT NOT NULL,
                direction TEXT NOT NULL,
                peer TEXT NOT NULL,
                description TEXT NOT NULL,
                raw_message TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);")
        conn.commit()
    finally:
        conn.close()


def _ensure_db():
    """Lazily initialize the database on first use."""
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


def _get_persistent_conn():
    """Return a persistent SQLite connection, creating it if needed."""
    global _persistent_conn
    if _persistent_conn is None:
        path = _get_db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _persistent_conn = sqlite3.connect(path, timeout=5)
        _persistent_conn.execute("PRAGMA journal_mode=WAL;")
    return _persistent_conn


def store_message(component, direction, peer, description, raw):
    """Insert one message row using a persistent connection."""
    global _insert_count
    _ensure_db()

    if isinstance(raw, dict):
        raw_str = json.dumps(raw, default=str)
    else:
        raw_str = str(raw) if raw is not None else None

    if raw_str and len(raw_str) > _MAX_RAW_LEN:
        raw_str = raw_str[:_MAX_RAW_LEN] + "..."

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    conn = _get_persistent_conn()
    conn.execute(
        "INSERT INTO messages (timestamp, component, direction, peer, description, raw_message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, component, direction, peer, description, raw_str),
    )
    conn.commit()

    should_cleanup = False
    with _insert_lock:
        _insert_count += 1
        if _insert_count >= _CLEANUP_EVERY:
            _insert_count = 0
            should_cleanup = True

    if should_cleanup:
        try:
            cleanup()
        except Exception as e:
            logger.warning(f"message_store error: {e}")


def query_recent(limit=200):
    """Return the last *limit* messages as a list of dicts, newest first."""
    _ensure_db()
    path = _get_db_path()
    if not os.path.exists(path):
        return []

    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, timestamp, component, direction, peer, description, raw_message "
            "FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cleanup(max_age_hours=24):
    """Delete messages older than *max_age_hours*."""
    path = _get_db_path()
    if not os.path.exists(path):
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()

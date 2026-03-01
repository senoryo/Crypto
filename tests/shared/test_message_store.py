"""Tests for shared/message_store.py — SQLite-backed message tracing."""

import os
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_message_store(tmp_path):
    """Redirect message_store to a temporary database for each test."""
    import shared.message_store as ms

    db_file = str(tmp_path / "test_messages.db")
    # Reset module-level state so init_db runs fresh
    ms._db_initialized = False
    ms._insert_count = 0
    # Close and reset persistent connection so it reconnects to the new db
    if ms._persistent_conn is not None:
        try:
            ms._persistent_conn.close()
        except Exception:
            pass
        ms._persistent_conn = None
    with patch.object(ms, "_db_path", None):
        with patch("shared.message_store._get_db_path", return_value=db_file):
            ms._db_initialized = False
            yield db_file
    # Reset again after test
    ms._db_initialized = False
    ms._insert_count = 0
    if ms._persistent_conn is not None:
        try:
            ms._persistent_conn.close()
        except Exception:
            pass
        ms._persistent_conn = None


@pytest.fixture
def ms():
    """Return the message_store module."""
    import shared.message_store as ms
    return ms


class TestInitDb:

    def test_creates_database_file(self, ms, isolate_message_store):
        ms.init_db()
        assert os.path.exists(isolate_message_store)

    def test_creates_messages_table(self, ms, isolate_message_store):
        ms.init_db()
        conn = sqlite3.connect(isolate_message_store)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        tables = cursor.fetchall()
        conn.close()
        assert len(tables) == 1

    def test_idempotent_init(self, ms, isolate_message_store):
        """Calling init_db twice should not raise."""
        ms.init_db()
        ms.init_db()
        assert os.path.exists(isolate_message_store)


class TestStoreMessage:

    def test_store_and_retrieve(self, ms, isolate_message_store):
        ms.store_message("OM", "RECV", "GUIBROKER", "order received", "raw data")
        rows = ms.query_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["component"] == "OM"
        assert rows[0]["direction"] == "RECV"
        assert rows[0]["peer"] == "GUIBROKER"
        assert rows[0]["description"] == "order received"
        assert rows[0]["raw_message"] == "raw data"

    def test_store_dict_raw(self, ms, isolate_message_store):
        """Dict raw messages should be JSON-serialized."""
        ms.store_message("OM", "SEND", "EXCHCONN", "forward order", {"key": "value"})
        rows = ms.query_recent(limit=10)
        assert len(rows) == 1
        assert '"key"' in rows[0]["raw_message"]

    def test_store_none_raw(self, ms, isolate_message_store):
        """None raw should be stored as None."""
        ms.store_message("OM", "SEND", "EXCHCONN", "test", None)
        rows = ms.query_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["raw_message"] is None

    def test_raw_truncation(self, ms, isolate_message_store):
        """Raw messages longer than _MAX_RAW_LEN should be truncated."""
        long_raw = "x" * 3000
        ms.store_message("OM", "RECV", "GUIBROKER", "big message", long_raw)
        rows = ms.query_recent(limit=10)
        assert len(rows) == 1
        assert len(rows[0]["raw_message"]) <= ms._MAX_RAW_LEN + 10  # "..." suffix


class TestQueryRecent:

    def test_empty_db(self, ms, isolate_message_store):
        ms._ensure_db()
        rows = ms.query_recent(limit=10)
        assert rows == []

    def test_ordering_newest_first(self, ms, isolate_message_store):
        ms.store_message("OM", "RECV", "A", "first", None)
        ms.store_message("OM", "RECV", "B", "second", None)
        ms.store_message("OM", "RECV", "C", "third", None)
        rows = ms.query_recent(limit=10)
        assert len(rows) == 3
        assert rows[0]["peer"] == "C"
        assert rows[2]["peer"] == "A"

    def test_limit_respected(self, ms, isolate_message_store):
        for i in range(5):
            ms.store_message("OM", "RECV", f"peer-{i}", f"msg-{i}", None)
        rows = ms.query_recent(limit=3)
        assert len(rows) == 3


class TestCleanup:

    def test_cleanup_removes_old_messages(self, ms, isolate_message_store):
        """Cleanup with max_age_hours=0 should remove all existing messages."""
        ms.store_message("OM", "RECV", "A", "old message", None)
        ms.cleanup(max_age_hours=0)
        rows = ms.query_recent(limit=10)
        assert len(rows) == 0

    def test_cleanup_on_nonexistent_db(self, ms, tmp_path):
        """Cleanup should not raise if the db file doesn't exist."""
        with patch("shared.message_store._get_db_path", return_value=str(tmp_path / "nope.db")):
            ms.cleanup()  # Should not raise

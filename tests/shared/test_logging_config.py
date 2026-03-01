"""Tests for shared/logging_config.py — component logging setup and helpers."""

import logging
import os
from unittest.mock import patch, MagicMock

import pytest

from shared.logging_config import setup_component_logging, log_recv, log_send, _truncate


class TestTruncate:

    def test_short_string_unchanged(self):
        result = _truncate("hello")
        assert result == "hello"

    def test_long_string_truncated(self):
        long_str = "x" * 1000
        result = _truncate(long_str)
        assert len(result) <= 503  # 500 + "..."
        assert result.endswith("...")

    def test_dict_serialized(self):
        result = _truncate({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_long_dict_truncated(self):
        big_dict = {f"key_{i}": "x" * 50 for i in range(100)}
        result = _truncate(big_dict)
        assert result.endswith("...")


class TestSetupComponentLogging:

    def test_returns_named_logger(self, tmp_path):
        with patch("shared.logging_config._LOGS_DIR", str(tmp_path)):
            logger = setup_component_logging("TEST_COMPONENT")
            assert logger.name == "TEST_COMPONENT"

    def test_creates_log_file(self, tmp_path):
        with patch("shared.logging_config._LOGS_DIR", str(tmp_path)):
            logger = setup_component_logging("TEST_COMP2")
            logger.info("test message")
            log_file = tmp_path / "TEST_COMP2.log"
            assert log_file.exists()

    def test_log_directory_created(self, tmp_path):
        log_dir = tmp_path / "subdir" / "logs"
        with patch("shared.logging_config._LOGS_DIR", str(log_dir)):
            setup_component_logging("TEST_COMP3")
            assert log_dir.exists()


class TestLogRecv:

    def test_logs_info_message(self):
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        with patch("shared.logging_config.message_store") as mock_store:
            mock_store.store_message = MagicMock()
            log_recv(mock_logger, "OM", "FIX NewOrder", "raw data")
        mock_logger.info.assert_called_once()
        assert "RECV" in mock_logger.info.call_args[0][0]
        assert "OM" in mock_logger.info.call_args[0][0]

    def test_skips_market_data(self):
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        log_recv(mock_logger, "MKTDATA", "market_data BTC/USD", "raw")
        mock_logger.info.assert_not_called()

    def test_skips_snapshot(self):
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        log_recv(mock_logger, "MKTDATA", "snapshot data", "raw")
        mock_logger.info.assert_not_called()


class TestLogSend:

    def test_logs_info_message(self):
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        with patch("shared.logging_config.message_store") as mock_store:
            mock_store.store_message = MagicMock()
            log_send(mock_logger, "GUIBROKER", "FIX ExecReport", "raw data")
        mock_logger.info.assert_called_once()
        assert "SEND" in mock_logger.info.call_args[0][0]
        assert "GUIBROKER" in mock_logger.info.call_args[0][0]

    def test_skips_position_update(self):
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        log_send(mock_logger, "GUI", "position_update BTC/USD", "raw")
        mock_logger.info.assert_not_called()

    def test_message_store_error_handled(self, capsys):
        """If message_store.store_message raises, error is printed to stderr."""
        mock_logger = MagicMock()
        mock_logger.name = "TEST"
        with patch("shared.logging_config.message_store") as mock_store:
            mock_store.store_message = MagicMock(side_effect=RuntimeError("db error"))
            log_send(mock_logger, "GUIBROKER", "FIX test", "raw")
        captured = capsys.readouterr()
        assert "Logging config error" in captured.err
        assert "db error" in captured.err

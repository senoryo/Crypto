"""
Shared logging configuration for all backend components.

Provides per-component file logging with full message tracing.
Each component gets its own rotating log file in logs/{COMPONENT}.log
plus a stderr stream handler to preserve existing console output.
"""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from shared import message_store

# Directory for all log files (relative to project root)
_LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

_MAX_RAW_LEN = 500  # Truncate raw messages beyond this length
_SKIP_PREFIXES = ("market_data", "snapshot", "position_snapshot", "position_update")


def setup_component_logging(component_name: str) -> logging.Logger:
    """Configure the root logger with file + stderr handlers for a component.

    - RotatingFileHandler -> logs/{component_name}.log  (10 MB, 3 backups)
    - StreamHandler -> stderr  (keeps existing console output)
    - Format: 2026-02-21 16:45:03.123 [COMPONENT] LEVEL: message

    Returns the named logger for the component.
    """
    os.makedirs(_LOGS_DIR, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path = os.path.join(_LOGS_DIR, f"{component_name}.log")

    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Remove any handlers added by prior basicConfig calls
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    return logging.getLogger(component_name)


def _truncate(raw) -> str:
    """Return a string representation of *raw*, truncated to _MAX_RAW_LEN."""
    if isinstance(raw, dict):
        s = json.dumps(raw, default=str)
    else:
        s = str(raw)
    if len(s) > _MAX_RAW_LEN:
        return s[:_MAX_RAW_LEN] + "..."
    return s


def log_recv(logger: logging.Logger, source: str, description: str, raw) -> None:
    """Log a received message.

    Example output::

        RECV from OM: FIX NewOrderSingle cl=GUI-1 BUY 1.0 SOL/USD LMT@178
          raw: {"fix":{"8":"FIX.4.4","35":"D",...}}
    """
    if description.startswith(_SKIP_PREFIXES):
        return
    logger.info(f"RECV from {source}: {description}")
    logger.debug(f"  raw: {_truncate(raw)}")
    try:
        message_store.store_message(logger.name, "RECV", source, description, raw)
    except Exception as e:
        import sys
        print(f"Logging config error: {e}", file=sys.stderr)


def log_send(logger: logging.Logger, destination: str, description: str, raw) -> None:
    """Log a sent message.

    Example output::

        SEND to GUIBROKER: FIX ExecReport NEW order=OM-000001
          raw: {"fix":{"8":"FIX.4.4","35":"8",...}}
    """
    if description.startswith(_SKIP_PREFIXES):
        return
    logger.info(f"SEND to {destination}: {description}")
    logger.debug(f"  raw: {_truncate(raw)}")
    try:
        message_store.store_message(logger.name, "SEND", destination, description, raw)
    except Exception as e:
        import sys
        print(f"Logging config error: {e}", file=sys.stderr)

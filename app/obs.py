"""Lightweight structured request logging (JSON lines).

Every HTTP attempt made through ``BaseCrawler.request()`` is recorded as one
JSON object per line in ``logs/crawl.jsonl`` (rotating). This makes it easy to
see which source is flaky: status codes, whether a proxy was used, how many
retries happened, and latency.

Configure via env vars:
    TRENDRADAR_LOG          Path to the log file. Default: <project>/logs/crawl.jsonl
    TRENDRADAR_LOG_CONSOLE  "1"/"true" to also echo records to stderr.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "crawl.jsonl"
LOG_PATH = Path(os.getenv("TRENDRADAR_LOG", str(_DEFAULT_PATH)))

_logger: logging.Logger | None = None


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("trendradar.crawl")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        if _truthy(os.getenv("TRENDRADAR_LOG_CONSOLE")):
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(console)
    _logger = logger
    return logger


def log_request(**fields: Any) -> None:
    """Write one structured request record."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        _get_logger().info(json.dumps(record, ensure_ascii=False))
    except Exception:
        # Logging must never break crawling.
        pass


def read_recent(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent log records (parsed), newest first."""
    if not LOG_PATH.exists():
        return []
    try:
        with LOG_PATH.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out

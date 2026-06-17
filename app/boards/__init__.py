"""Topic boards: configurable per-topic feeds, filters, and digests.

A board groups one or more feeds (RSS, Google News query, or an existing
crawler) under a single topic and applies include/exclude filters. Results
are stored as Articles (dedup'd by URL within the board).

Boards are defined in ``boards.yaml`` next to this file, so you can add a new
board without writing any Python.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Match ${VAR} or ${VAR:default value with spaces and / etc.}
# Only allow upper-case + digits + underscore in VAR name (standard env var rules).
_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")


def _expand_env(obj: Any) -> Any:
    """Recursively substitute ${VAR} / ${VAR:default} in string values.

    Used on boards.yaml so secrets / personal info (SEC contact email, etc.)
    can live in .env instead of being committed to the repo.
    """
    if isinstance(obj, str):
        def _sub(m: re.Match[str]) -> str:
            var = m.group(1)
            default = m.group(2)
            env_val = os.getenv(var)
            if env_val is not None and env_val != "":
                return env_val
            return default if default is not None else ""
        return _ENV_VAR_RE.sub(_sub, obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


CONFIG_PATH = Path(
    os.getenv("TRENDRADAR_BOARDS_CONFIG")
    or Path(__file__).resolve().parent / "boards.yaml"
)


@dataclass
class FeedConfig:
    """A single data feed inside a board."""

    type: str  # "rss" | "google_news" | "crawler" | "json_api"
    label: str = ""  # human-readable, shown in the dashboard
    # RSS / json_api share `url`
    url: str = ""
    # Google News
    query: str = ""
    lang: str = "zh-CN"
    country: str = "CN"
    # Existing crawler
    source: str = ""
    # json_api
    list_path: str = ""
    fields: dict = field(default_factory=dict)
    url_template: str = ""
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    # Cookie warmup: GET this URL first (in the same httpx client) to seed
    # session cookies before the real request. Best-effort; ignored on error.
    warmup_url: str = ""
    # Per-feed cap on the number of items returned by this feed. Useful for
    # firehoses (arxiv cs.CL emits ~150/day) so they don't drown out smaller
    # feeds in the same board. 0 = no cap (keep all items the feed returns).
    limit: int = 0


@dataclass
class Board:
    key: str
    name: str
    description: str = ""
    feeds: list[FeedConfig] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_items: int = 80
    # tags: {tag_name: [keyword, keyword, ...]}. For the rule-based tagger
    # keywords are matched as substrings; for the LLM tagger they're passed
    # as semantic hints (the tag NAME is the contract either way).
    tags: dict[str, list[str]] = field(default_factory=dict)
    # similarity threshold for event-clustering (0..1). 0.6 is a sensible
    # default for char-bigram Jaccard on news headlines.
    cluster_threshold: float = 0.6
    # Tagger backend: "rule" (default, cheap, deterministic) or "llm"
    # (calls a chat completion API; needs TRENDRADAR_LLM_API_KEY env var).
    tagger: str = "rule"
    # Summarizer backend: "none" (default) or "llm" (calls chat completion API
    # per article; needs TRENDRADAR_LLM_API_KEY). Articles get llm_summary
    # populated; existing summary is left untouched.
    summarizer: str = "none"


def _parse_feed(raw: dict[str, Any]) -> FeedConfig:
    return FeedConfig(
        type=raw.get("type", "rss"),
        label=raw.get("label", ""),
        url=raw.get("url", ""),
        query=raw.get("query", ""),
        lang=raw.get("lang", "zh-CN"),
        country=raw.get("country", "CN"),
        source=raw.get("source", ""),
        list_path=raw.get("list_path", ""),
        fields=dict(raw.get("fields") or {}),
        url_template=raw.get("url_template", ""),
        method=raw.get("method", "GET"),
        headers=dict(raw.get("headers") or {}),
        warmup_url=raw.get("warmup_url", ""),
        limit=int(raw.get("limit", 0) or 0),
    )


def _parse_board(raw: dict[str, Any]) -> Board:
    raw_tags = raw.get("tags") or {}
    parsed_tags: dict[str, list[str]] = {}
    if isinstance(raw_tags, dict):
        for name, kws in raw_tags.items():
            if isinstance(kws, list):
                parsed_tags[str(name)] = [str(k) for k in kws]
    return Board(
        key=raw["key"],
        name=raw.get("name", raw["key"]),
        description=raw.get("description", ""),
        feeds=[_parse_feed(f) for f in (raw.get("feeds") or [])],
        include=list(raw.get("include") or []),
        exclude=list(raw.get("exclude") or []),
        max_items=int(raw.get("max_items", 80)),
        tags=parsed_tags,
        cluster_threshold=float(raw.get("cluster_threshold", 0.6)),
        tagger=str(raw.get("tagger", "rule")).lower(),
        summarizer=str(raw.get("summarizer", "none")).lower(),
    )


def load_boards() -> list[Board]:
    """Load all board definitions from boards.yaml (re-read each call so
    edits don't require a server restart)."""
    if not CONFIG_PATH.exists():
        return []
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or []
    raw = _expand_env(raw)
    if isinstance(raw, dict):  # tolerate top-level {boards: [...]} too
        raw = raw.get("boards") or []
    return [_parse_board(b) for b in raw]


def get_board(key: str) -> Board | None:
    for b in load_boards():
        if b.key == key:
            return b
    return None

"""Feed fetchers for the board system.

Four feed types are supported:

* ``rss``         - any RSS 2.0 OR Atom URL (stdlib XML parser).
* ``google_news`` - Google News RSS search for an arbitrary query / locale.
* ``crawler``     - reuse the latest snapshot of an existing homepage crawler
                    (e.g. ``weibo``); no network call, just filter what we
                    already pulled.
* ``json_api``    - any JSON HTTP endpoint; you declare a path to the list of
                    items and a per-item field map in boards.yaml. Used to
                    plug in things like WallStreetCN's 7x24 livenews.

All HTTP fetchers piggy-back on ``BaseCrawler.get()`` to get retry +
proxy rotation + structured logging for free.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote

from ..crawlers import get_crawler
from ..crawlers.base import BaseCrawler
from ..db import SessionLocal
from ..models import Snapshot, Topic
from . import FeedConfig


@dataclass
class Article:
    """A single article fetched from a feed (not yet persisted)."""

    title: str
    url: str
    summary: str = ""
    published_at: Optional[datetime] = None
    source_label: str = ""
    score: Optional[float] = None
    extra: dict | None = None


# ---------------------------------------------------------------------------
# RSS / Atom fetcher (also used by Google News)
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _coerce_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_rfc822(s: str) -> datetime | None:
    try:
        return _coerce_aware(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        return None


def _parse_iso8601(s: str) -> datetime | None:
    """Atom feeds typically use RFC 3339 / ISO 8601 timestamps. Python's
    fromisoformat handles "+00:00" but not "Z", so normalize first."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return _coerce_aware(datetime.fromisoformat(s))
    except ValueError:
        return _parse_rfc822(s)  # last-resort fallback


def _extract_text(elem: ET.Element) -> str:
    """Return concatenated text content of an element (handles nested tags)."""
    if elem is None:
        return ""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.text:
            parts.append(child.text)
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _parse_rss(xml_text: str) -> list[Article]:
    """Parse both RSS 2.0 (<item>) and Atom (<entry>) feeds."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[Article] = []
    for item in root.iter():
        tag = _local(item.tag).lower()
        if tag not in ("item", "entry"):
            continue
        is_atom = tag == "entry"

        title = ""
        url = ""
        summary = ""
        published_raw = ""

        for child in item:
            name = _local(child.tag).lower()
            if name == "title" and not title:
                title = _extract_text(child)
            elif name == "link" and not url:
                # Atom: <link href="..."/>;  RSS: <link>url</link>
                href = child.attrib.get("href")
                url = (href or (child.text or "")).strip()
            elif name == "guid" and not url:
                url = (child.text or "").strip()
            elif name in ("description", "summary", "content") and not summary:
                summary = _extract_text(child)[:600]
            elif name in ("pubdate", "published", "updated", "date") and not published_raw:
                published_raw = (child.text or "").strip()

        title = (title or "").strip()
        if not title or not url:
            continue

        if published_raw:
            published = _parse_iso8601(published_raw) if is_atom else (
                _parse_rfc822(published_raw) or _parse_iso8601(published_raw)
            )
        else:
            published = None

        out.append(
            Article(
                title=title,
                url=url,
                summary=summary,
                published_at=published,
            )
        )
    return out


class _FetcherCrawler(BaseCrawler):
    """Thin BaseCrawler subclass so feed calls go through retry + proxy +
    log machinery. ``key`` is set per-fetch so logs show which feed it was.

    Board fetches are batch background ops; we can afford a longer timeout
    than the interactive 12s default to tolerate slow CN sites.
    """

    region = "Board"
    timeout = 20.0

    def __init__(self, key: str) -> None:
        self.key = key  # set as an instance attribute, not class attribute


def _merge_headers(crawler: BaseCrawler, headers: dict | None) -> dict | None:
    """Layer caller-supplied headers on top of the crawler's defaults so
    UA/Accept-Language survive while custom keys (Cookie, Referer, etc.)
    get added on top."""
    if not headers:
        return None
    merged = crawler.headers()
    merged.update(headers)
    return merged


async def fetch_rss(
    url: str, label: str, headers: dict | None = None
) -> list[Article]:
    crawler = _FetcherCrawler(key=f"board:{label}")
    extra = _merge_headers(crawler, headers)
    if extra is not None:
        resp = await crawler.get(url, headers=extra)
    else:
        resp = await crawler.get(url)
    items = _parse_rss(resp.text)
    for it in items:
        it.source_label = label
    return items


async def fetch_google_news(query: str, lang: str, country: str, label: str) -> list[Article]:
    """Google News RSS — supports arbitrary search query + locale.

    Google News returns wrapped/tracking redirect links; we keep them as-is
    because they still resolve in a browser.
    """
    if not query.strip():
        return []
    # hl=zh-CN gl=CN ceid=CN:zh-Hans ; for English use hl=en-US gl=US ceid=US:en
    ceid_lang = lang.split("-")[0] if lang else "en"
    ceid = f"{country}:{ceid_lang}"
    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + f"&hl={lang}&gl={country}&ceid={ceid}"
    )
    return await fetch_rss(url, label or f"google_news:{query[:30]}")


# ---------------------------------------------------------------------------
# Generic JSON-API fetcher
# ---------------------------------------------------------------------------


def _dig(obj: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts / lists. Returns None on miss.

    Supports list indexing with numeric segments, e.g. ``data.items.0.title``.
    """
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _to_datetime(value: Any) -> datetime | None:
    """Best-effort conversion of common timestamp shapes (int unix seconds,
    int unix millis, RFC 3339 / ISO 8601 string, RFC 822 string)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Heuristic: if the value is big enough to be millis, treat as millis.
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return _to_datetime(int(s))
        return _parse_iso8601(s) or _parse_rfc822(s)
    return None


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


async def fetch_json_api(
    *,
    url: str,
    label: str,
    list_path: str = "",
    fields: dict[str, str] | None = None,
    url_template: str = "",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    warmup_url: str = "",
) -> list[Article]:
    """Fetch a JSON HTTP endpoint and project it into Articles.

    Args:
      url: HTTP(S) endpoint, full URL.
      label: human-readable label for the feed (shown in dashboard + logs).
      list_path: dotted path from the JSON root to the list of items.
        Example: ``data.items`` for ``{"data": {"items": [...]}}``.
        Use the empty string if the response is already a list.
      fields: per-item field map. Keys MUST be a subset of
        {"title","url","summary","published_at"}; values are dotted paths
        inside one item dict. ``title`` is required; if missing on an item,
        we fall back to ``summary[:80]``.
      url_template: optional ``"https://.../{id}"`` template for items that
        don't carry a URL field (we'll str.format it with the item dict).
      method: HTTP method; only "GET" / "POST" are useful here.
      headers: extra HTTP headers to merge on top of BaseCrawler defaults.
      warmup_url: optional URL to GET first inside the same httpx client to
        seed session cookies (e.g. Xueqiu requires a homepage visit).
    """
    if not url.strip():
        return []
    fields = fields or {}
    crawler = _FetcherCrawler(key=f"board:{label}")
    req_kwargs: dict[str, Any] = {}
    extra = _merge_headers(crawler, headers)
    if extra is not None:
        req_kwargs["headers"] = extra
    if warmup_url:
        req_kwargs["warmup_url"] = warmup_url
    resp = await crawler.request(method, url, **req_kwargs)

    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError):
        # Some endpoints return JSONP / wrapped JS; try to peel it off.
        text = resp.text.strip()
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or first >= last:
            return []
        try:
            payload = json.loads(text[first : last + 1])
        except (ValueError, json.JSONDecodeError):
            return []

    raw_items = _dig(payload, list_path) if list_path else payload
    if not isinstance(raw_items, list):
        return []

    title_path = fields.get("title", "title")
    url_path = fields.get("url", "url")
    summary_path = fields.get("summary", "")
    published_path = fields.get("published_at", "")

    out: list[Article] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = _stringify(_dig(raw, title_path))
        summary = _stringify(_dig(raw, summary_path)) if summary_path else ""
        if not title and summary:
            # WallStreetCN-style: livenews items often have no title, just content.
            title = summary[:80]
        if not title:
            continue
        url_val = _stringify(_dig(raw, url_path)) if url_path else ""
        if not url_val and url_template:
            try:
                url_val = url_template.format(**raw)
            except (KeyError, IndexError, ValueError):
                url_val = ""
        if not url_val:
            continue
        published = _to_datetime(_dig(raw, published_path)) if published_path else None
        out.append(
            Article(
                title=title,
                url=url_val,
                summary=summary[:600],
                published_at=published,
                source_label=label,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Existing-crawler fetcher
# ---------------------------------------------------------------------------


def fetch_from_crawler(source_key: str, label: str) -> list[Article]:
    """Pull articles from the latest successful snapshot of an existing
    homepage crawler (e.g. ``weibo``). No network call - reads from SQLite.
    """
    crawler = get_crawler(source_key)
    if crawler is None:
        return []
    with SessionLocal() as session:
        snap = (
            session.query(Snapshot)
            .filter(Snapshot.source_key == source_key, Snapshot.status == "ok")
            .order_by(Snapshot.fetched_at.desc())
            .first()
        )
        if not snap:
            return []
        topics = (
            session.query(Topic)
            .filter(Topic.snapshot_id == snap.id)
            .order_by(Topic.rank)
            .all()
        )
        # SQLAlchemy may return naive datetimes for SQLite TIMESTAMP columns;
        # normalize to UTC-aware to keep sorting/comparisons consistent.
        fetched_at = _coerce_aware(snap.fetched_at)
        return [
            Article(
                title=t.title,
                url=t.url or f"https://example.com/{source_key}/{t.id}",
                summary="",
                published_at=fetched_at,
                source_label=label or source_key,
                score=t.score,
            )
            for t in topics
            if t.title
        ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def fetch_feed(feed: FeedConfig) -> list[Article]:
    """Dispatch to the right fetcher based on feed.type, then apply
    feed.limit (if set) so firehose-style sources don't dominate the board.
    Items are assumed to be returned in newest-first order (or at least a
    reasonable order) by each fetcher."""
    if feed.type == "rss":
        items = await fetch_rss(
            feed.url, feed.label or feed.url, headers=feed.headers or None
        )
    elif feed.type == "google_news":
        items = await fetch_google_news(
            feed.query, feed.lang, feed.country, feed.label or f"GNews:{feed.query[:30]}"
        )
    elif feed.type == "crawler":
        items = fetch_from_crawler(feed.source, feed.label or feed.source)
    elif feed.type == "json_api":
        items = await fetch_json_api(
            url=feed.url,
            label=feed.label or feed.url,
            list_path=feed.list_path,
            fields=feed.fields,
            url_template=feed.url_template,
            method=feed.method or "GET",
            headers=feed.headers,
            warmup_url=feed.warmup_url,
        )
    else:
        raise ValueError(f"unknown feed type: {feed.type}")

    if feed.limit and feed.limit > 0:
        items = items[: feed.limit]
    return items

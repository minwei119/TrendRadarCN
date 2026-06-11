"""Orchestrate crawls and provide query helpers for the API layer."""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import desc, func, select

from .crawlers import get_crawler, iter_crawlers
from .crawlers.base import BaseCrawler, TopicItem
from .db import SessionLocal
from .models import Snapshot, Source, Topic


def _serialize_topic(t: Topic) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if t.extra:
        try:
            extra = json.loads(t.extra)
        except Exception:
            extra = {}
    return {
        "rank": t.rank,
        "title": t.title,
        "url": t.url,
        "score": t.score,
        "extra": extra,
    }


async def _run_one(crawler: BaseCrawler) -> dict[str, Any]:
    start = time.perf_counter()
    error: str | None = None
    items: list[TopicItem] = []
    try:
        items = await crawler.fetch()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.perf_counter() - start) * 1000)

    with SessionLocal() as session:
        snapshot = Snapshot(
            source_key=crawler.key,
            fetched_at=datetime.now(timezone.utc),
            status="ok" if error is None else "error",
            item_count=len(items),
            duration_ms=duration_ms,
            error=error,
        )
        session.add(snapshot)
        session.flush()
        for rank, item in enumerate(items, start=1):
            session.add(
                Topic(
                    snapshot_id=snapshot.id,
                    rank=rank,
                    title=item.title.strip()[:500],
                    url=item.url,
                    score=item.score,
                    extra=json.dumps(item.extra, ensure_ascii=False) if item.extra else None,
                )
            )
        session.commit()

        return {
            "source_key": crawler.key,
            "snapshot_id": snapshot.id,
            "status": snapshot.status,
            "item_count": snapshot.item_count,
            "duration_ms": duration_ms,
            "error": error,
        }


async def crawl_all() -> list[dict[str, Any]]:
    crawlers = list(iter_crawlers())
    return await asyncio.gather(*(_run_one(c) for c in crawlers))


async def crawl_all_iter(self_print: bool = False):
    """Crawl every source concurrently, yielding each result as it completes
    (rather than waiting for the slowest source). Useful for live CLI output."""
    crawlers = list(iter_crawlers())
    tasks = [asyncio.create_task(_run_one(c)) for c in crawlers]
    for coro in asyncio.as_completed(tasks):
        yield await coro


async def crawl_one(source_key: str) -> dict[str, Any] | None:
    crawler = get_crawler(source_key)
    if not crawler:
        return None
    return await _run_one(crawler)


def list_sources() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        sources = session.scalars(select(Source).order_by(Source.key)).all()
        latest_subq = (
            select(
                Snapshot.source_key,
                func.max(Snapshot.fetched_at).label("last_at"),
            )
            .group_by(Snapshot.source_key)
            .subquery()
        )
        last_map = {row.source_key: row.last_at for row in session.execute(select(latest_subq)).all()}
        return [
            {
                "key": s.key,
                "display_name": s.display_name,
                "region": s.region,
                "url": s.url,
                "last_fetched_at": (
                    last_map[s.key].isoformat() if s.key in last_map and last_map[s.key] else None
                ),
            }
            for s in sources
        ]


def latest_topics(source_key: str, limit: int = 50) -> dict[str, Any] | None:
    with SessionLocal() as session:
        snap = session.scalars(
            select(Snapshot)
            .where(Snapshot.source_key == source_key, Snapshot.status == "ok")
            .order_by(desc(Snapshot.fetched_at))
            .limit(1)
        ).first()
        if not snap:
            return None
        topics = session.scalars(
            select(Topic).where(Topic.snapshot_id == snap.id).order_by(Topic.rank).limit(limit)
        ).all()
        return {
            "source_key": snap.source_key,
            "snapshot_id": snap.id,
            "fetched_at": snap.fetched_at.isoformat(),
            "item_count": snap.item_count,
            "topics": [_serialize_topic(t) for t in topics],
        }


def latest_topics_all(limit_per_source: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in list_sources():
        snap = latest_topics(source["key"], limit=limit_per_source)
        if snap:
            snap["display_name"] = source["display_name"]
            out.append(snap)
    return out


def topic_trend(title: str, source_key: str | None = None, hours: int = 48) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        stmt = (
            select(Topic, Snapshot)
            .join(Snapshot, Topic.snapshot_id == Snapshot.id)
            .where(Topic.title == title, Snapshot.fetched_at >= cutoff)
        )
        if source_key:
            stmt = stmt.where(Snapshot.source_key == source_key)
        stmt = stmt.order_by(Snapshot.fetched_at)
        rows = session.execute(stmt).all()
        return [
            {
                "source_key": snap.source_key,
                "fetched_at": snap.fetched_at.isoformat(),
                "rank": topic.rank,
                "score": topic.score,
            }
            for topic, snap in rows
        ]


def aggregate_top(hours: int = 24, limit: int = 30) -> list[dict[str, Any]]:
    """Aggregate top titles across all sources within last N hours.

    A title's aggregate score = sum across sources of (sources*2 - rank + 1),
    so rank 1 always contributes most, and a title appearing on many sources is
    weighted heavily.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"score": 0.0, "sources": set(), "best_rank": 99, "latest_url": None}
    )
    with SessionLocal() as session:
        stmt = (
            select(Topic, Snapshot)
            .join(Snapshot, Topic.snapshot_id == Snapshot.id)
            .where(Snapshot.fetched_at >= cutoff, Snapshot.status == "ok")
        )
        for topic, snap in session.execute(stmt).all():
            row = bucket[topic.title]
            weight = max(0, 51 - (topic.rank or 50))
            row["score"] += weight
            row["sources"].add(snap.source_key)
            if (topic.rank or 99) < row["best_rank"]:
                row["best_rank"] = topic.rank
            if topic.url:
                row["latest_url"] = topic.url

    aggregated = [
        {
            "title": title,
            "weighted_score": data["score"],
            "source_count": len(data["sources"]),
            "sources": sorted(data["sources"]),
            "best_rank": data["best_rank"],
            "url": data["latest_url"],
        }
        for title, data in bucket.items()
    ]
    aggregated.sort(key=lambda r: (-r["source_count"], -r["weighted_score"]))
    return aggregated[:limit]


def recent_snapshots(source_key: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        stmt = select(Snapshot).order_by(desc(Snapshot.fetched_at)).limit(limit)
        if source_key:
            stmt = (
                select(Snapshot)
                .where(Snapshot.source_key == source_key)
                .order_by(desc(Snapshot.fetched_at))
                .limit(limit)
            )
        rows = session.scalars(stmt).all()
        return [
            {
                "id": s.id,
                "source_key": s.source_key,
                "fetched_at": s.fetched_at.isoformat(),
                "status": s.status,
                "item_count": s.item_count,
                "duration_ms": s.duration_ms,
                "error": s.error,
            }
            for s in rows
        ]

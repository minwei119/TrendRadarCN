"""Board orchestration: run a board's feeds, filter, persist, query."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from ..models import Article as ArticleORM, BoardRun
from . import Board, get_board, load_boards
from .dedup import assign_cluster
from .fetchers import Article, fetch_feed
from .llm_tagger import is_configured as llm_configured, llm_tag_articles
from .tagger import tag_article


def _matches_filters(text: str, include: list, exclude: list) -> bool:
    """Substring match (case-insensitive) over title+summary.

    include/exclude items are stringified before matching so that YAML lists
    containing bare numbers (e.g. stock codes ``0700`` / ``9988``) work as
    naturally as quoted strings.
    """
    haystack = text.lower()
    if exclude and any(str(e).lower() in haystack for e in exclude):
        return False
    if include:
        return any(str(i).lower() in haystack for i in include)
    return True


async def run_board(board_key: str) -> dict[str, Any]:
    board = get_board(board_key)
    if board is None:
        return {"board_key": board_key, "status": "error", "error": "board not found"}

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    feeds_total = len(board.feeds)
    feeds_ok = 0
    all_articles: list[Article] = []
    errors: list[str] = []

    async def _one(feed):
        feed_started = time.perf_counter()
        label = feed.label or feed.url or feed.query or feed.source
        try:
            items = await fetch_feed(feed)
            elapsed = int((time.perf_counter() - feed_started) * 1000)
            print(
                f"  [feed-ok] {board.key:14s} {label:35s} items={len(items):>3} {elapsed:>5}ms",
                flush=True,
            )
            return feed, items, None
        except Exception as exc:  # noqa: BLE001 - we want every failure recorded
            elapsed = int((time.perf_counter() - feed_started) * 1000)
            print(
                f"  [feed-ERR] {board.key:14s} {label:35s} {type(exc).__name__:20s} {elapsed:>5}ms",
                flush=True,
            )
            return feed, [], f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(_one(f) for f in board.feeds))
    for feed, items, err in results:
        if err:
            errors.append(f"{feed.type}:{feed.label or feed.url or feed.query}: {err}")
        else:
            feeds_ok += 1
            all_articles.extend(items)

    # Apply include/exclude filters (title + summary).
    filtered: list[Article] = []
    for a in all_articles:
        text = f"{a.title} {a.summary}"
        if _matches_filters(text, board.include, board.exclude):
            filtered.append(a)

    # Sort by published_at (newest first) and cap. Force every key to be
    # tz-aware UTC so naive vs aware datetimes can't crash the comparison.
    _MIN = datetime.min.replace(tzinfo=timezone.utc)

    def _sort_key(a) -> datetime:
        pub = a.published_at
        if pub is None:
            return _MIN
        if pub.tzinfo is None:
            return pub.replace(tzinfo=timezone.utc)
        return pub.astimezone(timezone.utc)

    filtered.sort(key=_sort_key, reverse=True)
    filtered = filtered[: board.max_items]

    # Persist; ignore IntegrityError to skip dups (we have unique on board+url
    # and board+title). For each successfully inserted article we ALSO compute
    # its tags (rule-based inline, or LLM in a batch after the loop) and
    # assign an event-cluster id (see app/boards/dedup.py).
    new_count = 0
    use_llm = board.tagger == "llm" and bool(board.tags) and llm_configured()
    new_for_llm: list[tuple[int, str, str]] = []  # (id, title, summary)
    with SessionLocal() as session:
        # Candidate pool for clustering: same board, last 48h. Pulled once;
        # then we append to it as we insert so back-to-back near-dupes within
        # the same run also collapse into one cluster.
        cluster_window_start = datetime.now(timezone.utc) - timedelta(hours=48)
        cluster_pool: list[tuple[int, int, str]] = [
            (r.id, r.cluster_id or r.id, r.title)
            for r in session.scalars(
                select(ArticleORM)
                .where(ArticleORM.board_key == board.key)
                .where(ArticleORM.fetched_at >= cluster_window_start)
            ).all()
        ]

        for a in filtered:
            text = f"{a.title} {a.summary or ''}"
            # Rule-based path tags inline; LLM path defers and batches below.
            if use_llm:
                tags: list[str] = []
            else:
                tags = tag_article(text, board.tags) if board.tags else []
            try:
                row = ArticleORM(
                    board_key=board.key,
                    source_label=a.source_label,
                    title=a.title[:500],
                    url=a.url,
                    summary=a.summary or None,
                    published_at=a.published_at,
                    score=a.score,
                    extra=json.dumps(a.extra, ensure_ascii=False) if a.extra else None,
                    tags=json.dumps(tags, ensure_ascii=False) if tags else None,
                )
                session.add(row)
                session.flush()  # assign row.id without committing yet
                cid = assign_cluster(row.title, cluster_pool, board.cluster_threshold)
                row.cluster_id = cid if cid is not None else row.id
                session.commit()
                new_count += 1
                cluster_pool.append((row.id, row.cluster_id, row.title))
                if use_llm:
                    new_for_llm.append((row.id, row.title, a.summary or ""))
            except IntegrityError:
                session.rollback()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                errors.append(f"persist:{a.title[:40]}: {exc}")

        # LLM tagging: one batched pass over everything we just inserted.
        llm_stats: dict[str, Any] | None = None
        if use_llm and new_for_llm:
            try:
                tag_map, stats = await llm_tag_articles(new_for_llm, board.tags)
                tagged_n = 0
                for aid, tags_list in tag_map.items():
                    if not tags_list:
                        continue
                    row = session.get(ArticleORM, aid)
                    if row is not None:
                        row.tags = json.dumps(tags_list, ensure_ascii=False)
                        tagged_n += 1
                if tagged_n:
                    session.commit()
                stats["tagged"] = tagged_n
                llm_stats = stats
                print(
                    f"  llm[{board.key}]: tagged={tagged_n}/{len(new_for_llm)} "
                    f"batches={stats.get('batches',0)} "
                    f"tokens={stats.get('prompt_tokens',0)}+{stats.get('completion_tokens',0)} "
                    f"usd={stats.get('cost_usd',0.0):.5f}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"llm-tagger:{type(exc).__name__}: {exc}")

        run = BoardRun(
            board_key=board.key,
            started_at=started_at,
            status="ok" if not errors else ("ok" if feeds_ok > 0 else "error"),
            feeds_total=feeds_total,
            feeds_ok=feeds_ok,
            articles_seen=len(filtered),
            articles_new=new_count,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error="\n".join(errors)[:2000] if errors else None,
        )
        session.add(run)
        session.commit()

    return {
        "board_key": board.key,
        "status": "ok" if feeds_ok > 0 else "error",
        "feeds_total": feeds_total,
        "feeds_ok": feeds_ok,
        "articles_seen": len(filtered),
        "articles_new": new_count,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "errors": errors[:5],  # truncate for summary
        "llm_stats": llm_stats,
    }


async def run_all_boards():
    """Run every defined board, yielding each result as it completes."""
    tasks = [asyncio.create_task(run_board(b.key)) for b in load_boards()]
    for coro in asyncio.as_completed(tasks):
        yield await coro


def list_boards_summary() -> list[dict[str, Any]]:
    """Boards + per-board last-run + article count, for the API/UI."""
    boards = load_boards()
    out = []
    with SessionLocal() as session:
        for b in boards:
            last_run = (
                session.query(BoardRun)
                .filter(BoardRun.board_key == b.key)
                .order_by(desc(BoardRun.started_at))
                .first()
            )
            total_articles = (
                session.query(ArticleORM)
                .filter(ArticleORM.board_key == b.key)
                .count()
            )
            out.append(
                {
                    "key": b.key,
                    "name": b.name,
                    "description": b.description,
                    "feed_count": len(b.feeds),
                    "include": b.include,
                    "exclude": b.exclude,
                    "tags": list(b.tags.keys()),
                    "last_run_at": last_run.started_at.isoformat() if last_run else None,
                    "last_articles_new": last_run.articles_new if last_run else 0,
                    "total_articles": total_articles,
                }
            )
    return out


def list_articles(
    board_key: str,
    hours: int = 48,
    limit: int = 80,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Recent articles for a board, grouped by event-cluster (newest first).

    For each cluster we return ONE row (the earliest article of the cluster),
    plus a list of all sources that reported the event and the total count.
    Articles without a cluster_id (legacy rows from before clustering existed)
    are treated as singletons.

    Args:
      hours: time window for "recent" (default 48h).
      limit: max number of cluster-rows to return.
      tag: optional tag filter — only clusters whose representative carries
        this tag are returned.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        stmt = (
            select(ArticleORM)
            .where(ArticleORM.board_key == board_key)
            .where(
                (ArticleORM.published_at >= cutoff)
                | (
                    (ArticleORM.published_at.is_(None))
                    & (ArticleORM.fetched_at >= cutoff)
                )
            )
            # Pull a generous slice so clustering can collapse correctly.
            .order_by(desc(ArticleORM.published_at), desc(ArticleORM.fetched_at))
            .limit(max(limit * 5, 200))
        )
        rows = session.scalars(stmt).all()

        # Group by cluster_id (fall back to id for legacy rows).
        # Each cluster keeps the article with the newest published_at as the
        # representative, plus the set of source labels & total count.
        groups: dict[int, dict[str, Any]] = {}
        for r in rows:
            cid = r.cluster_id or r.id
            tags = _decode_tags(r.tags)
            entry = groups.get(cid)
            ts = r.published_at or r.fetched_at
            if entry is None:
                groups[cid] = {
                    "rep": r,
                    "rep_ts": ts,
                    "sources": [r.source_label],
                    "tag_set": set(tags),
                    "count": 1,
                }
            else:
                entry["count"] += 1
                if r.source_label not in entry["sources"]:
                    entry["sources"].append(r.source_label)
                entry["tag_set"].update(tags)
                if ts and (entry["rep_ts"] is None or ts > entry["rep_ts"]):
                    entry["rep"] = r
                    entry["rep_ts"] = ts

        def _sort_key(g: dict[str, Any]):
            ts = g["rep_ts"]
            return ts or datetime.min.replace(tzinfo=timezone.utc)

        sorted_groups = sorted(groups.values(), key=_sort_key, reverse=True)
        if tag:
            sorted_groups = [g for g in sorted_groups if tag in g["tag_set"]]
        sorted_groups = sorted_groups[:limit]

        return [
            {
                "id": g["rep"].id,
                "title": g["rep"].title,
                "url": g["rep"].url,
                "summary": g["rep"].summary,
                "source": g["rep"].source_label,
                "published_at": g["rep"].published_at.isoformat()
                if g["rep"].published_at
                else None,
                "fetched_at": g["rep"].fetched_at.isoformat(),
                "score": g["rep"].score,
                "tags": sorted(g["tag_set"]),
                "cluster_count": g["count"],
                "cluster_sources": g["sources"],
            }
            for g in sorted_groups
        ]


def _decode_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(t) for t in v] if isinstance(v, list) else []
    except (ValueError, json.JSONDecodeError):
        return []


async def backfill_tags_and_clusters(
    verbose: bool = False,
) -> dict[str, dict[str, Any]]:
    """One-shot backfill for legacy articles inserted before tagging/clustering
    existed (or for rows that were created when a board had no tags yet).

    Idempotent: only touches rows where the relevant column is NULL. Safe to
    call on every server start — when there's nothing to do it's ~one SELECT.

    For boards with ``tagger: llm`` configured AND the LLM API key set, the
    tag backfill goes through the LLM (batched). Otherwise rule-based.

    Returns a per-board summary
    ``{board_key: {"tagged": N, "clustered": M, "llm": stats|None}}``.
    """
    boards = {b.key: b for b in load_boards()}
    summary: dict[str, dict[str, Any]] = {}
    if not boards:
        return summary
    with SessionLocal() as session:
        for board_key, board in boards.items():
            tagged_n = 0
            clustered_n = 0
            llm_stats: dict[str, Any] | None = None
            use_llm = board.tagger == "llm" and bool(board.tags) and llm_configured()

            # 1) Tag backfill — only for boards that have a tag config.
            if board.tags:
                rows = session.scalars(
                    select(ArticleORM)
                    .where(ArticleORM.board_key == board_key)
                    .where(ArticleORM.tags.is_(None))
                ).all()
                if rows:
                    if use_llm:
                        # LLM path: one batched call set.
                        articles = [(r.id, r.title, r.summary or "") for r in rows]
                        tag_map, stats = await llm_tag_articles(articles, board.tags)
                        for r in rows:
                            tags = tag_map.get(r.id, [])
                            if tags:
                                r.tags = json.dumps(tags, ensure_ascii=False)
                                tagged_n += 1
                        stats["tagged"] = tagged_n
                        llm_stats = stats
                    else:
                        for row in rows:
                            text = f"{row.title} {row.summary or ''}"
                            tags = tag_article(text, board.tags)
                            if tags:
                                row.tags = json.dumps(tags, ensure_ascii=False)
                                tagged_n += 1
                    session.commit()

            # 2) Cluster backfill — assign clusters to NULL rows by greedy
            #    nearest-neighbor against everything already clustered. We
            #    process oldest-first so the earliest article anchors each
            #    cluster (matches the live insert path).
            unclustered = session.scalars(
                select(ArticleORM)
                .where(ArticleORM.board_key == board_key)
                .where(ArticleORM.cluster_id.is_(None))
                .order_by(ArticleORM.id)
            ).all()
            if unclustered:
                pool: list[tuple[int, int, str]] = [
                    (r.id, r.cluster_id or r.id, r.title)
                    for r in session.scalars(
                        select(ArticleORM)
                        .where(ArticleORM.board_key == board_key)
                        .where(ArticleORM.cluster_id.is_not(None))
                    ).all()
                ]
                for row in unclustered:
                    cid = assign_cluster(row.title, pool, board.cluster_threshold)
                    row.cluster_id = cid if cid is not None else row.id
                    pool.append((row.id, row.cluster_id, row.title))
                    clustered_n += 1
                session.commit()

            summary[board_key] = {
                "tagged": tagged_n,
                "clustered": clustered_n,
                "llm": llm_stats,
                "tagger": board.tagger,
                "has_tags_config": bool(board.tags),
                "llm_key_set": llm_configured(),
            }
            if verbose:
                via = (
                    "llm" if use_llm
                    else ("rule" if board.tags
                          else "—  (no tags configured)")
                )
                cost = f" usd={llm_stats['cost_usd']:.5f}" if llm_stats else ""
                extra = ""
                if board.tagger == "llm" and not llm_configured():
                    extra = " [LLM key NOT set → fallback to rule]"
                print(
                    f"  backfill[{board_key}]: tagged={tagged_n} via {via}{cost} "
                    f"clustered={clustered_n}{extra}",
                    flush=True,
                )
                # Surface LLM failures inline. Otherwise tagged=0 looks like
                # "all batches succeeded but found nothing" instead of "all
                # batches errored". First error message is usually enough to
                # diagnose key/network/model-name issues.
                if llm_stats and llm_stats.get("errors"):
                    msgs = llm_stats.get("error_messages") or []
                    first = msgs[0] if msgs else "(no message)"
                    print(
                        f"     ! LLM failed on {llm_stats['errors']}/"
                        f"{llm_stats['batches']} batches; first error: {first}",
                        flush=True,
                    )
    return summary


def reset_board_tags(board_key: str) -> int:
    """NULL out the tags column for one board so it can be re-tagged from
    scratch (typically after switching tagger or changing the tag schema).
    Returns the number of rows reset.
    """
    with SessionLocal() as session:
        rows = session.scalars(
            select(ArticleORM).where(ArticleORM.board_key == board_key)
        ).all()
        n = 0
        for r in rows:
            if r.tags is not None:
                r.tags = None
                n += 1
        if n:
            session.commit()
        return n


def recent_board_runs(board_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        stmt = select(BoardRun).order_by(desc(BoardRun.started_at)).limit(limit)
        if board_key:
            stmt = (
                select(BoardRun)
                .where(BoardRun.board_key == board_key)
                .order_by(desc(BoardRun.started_at))
                .limit(limit)
            )
        return [
            {
                "id": r.id,
                "board_key": r.board_key,
                "started_at": r.started_at.isoformat(),
                "status": r.status,
                "feeds_total": r.feeds_total,
                "feeds_ok": r.feeds_ok,
                "articles_seen": r.articles_seen,
                "articles_new": r.articles_new,
                "duration_ms": r.duration_ms,
                "error": r.error,
            }
            for r in session.scalars(stmt).all()
        ]

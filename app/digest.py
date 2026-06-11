"""Build a daily HTML+text email digest of new board articles.

Usage:
    from app.digest import build_digest
    d = build_digest(hours=24)
    # d = {"subject": ..., "html": ..., "text": ..., "total_articles": int,
    #      "board_counts": {board_key: int, ...}}

Design:
- For each board, pull rows with ``fetched_at >= now - hours`` (i.e. newly
  ingested in the digest window — the daily scheduled run is the canonical
  caller, so "newness" = "ingested today").
- Group rows by ``cluster_id`` (legacy NULL rows are treated as singletons
  via fallback to ``id``). Per cluster keep the article with the newest
  ``fetched_at`` as representative, and record ``cluster_size``.
- Sort clusters by (cluster_size DESC, fetched_at DESC) and take top 8.
- Render both HTML (inline CSS only — many email clients drop <style>) and
  a plain-text fallback. Both versions are returned so the caller can build
  a multipart/alternative message.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .boards import Board, load_boards
from .db import SessionLocal
from .models import Article as ArticleORM


TOP_PER_BOARD = 8

# Stable palette for per-board accent badges. Picked one color per board key
# via ``hash(key) % len(palette)`` so the same board always gets the same
# color in the email — no random reshuffling between digests.
_BOARD_PALETTE = [
    "#dc2626",  # red
    "#2563eb",  # blue
    "#059669",  # green
    "#d97706",  # amber
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#db2777",  # pink
    "#475569",  # slate
]


def _board_color(key: str) -> str:
    return _BOARD_PALETTE[hash(key) % len(_BOARD_PALETTE)]


def _decode_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(t) for t in v] if isinstance(v, list) else []
    except (ValueError, json.JSONDecodeError):
        return []


@dataclass
class _Cluster:
    """One row in the rendered digest: a cluster's representative article
    plus aggregate metadata across the cluster (size, sources, tags).

    Holds plain values (not the ORM object) so rendering can happen after
    the SQLAlchemy session has closed.
    """

    title: str
    url: str
    source_label: str
    rep_ts: datetime
    cluster_size: int
    sources: list[str]
    tags: list[str]


def _fetch_clusters(board_key: str, hours: int) -> list[_Cluster]:
    """Return up to TOP_PER_BOARD clusters for a board, newest+busiest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        stmt = (
            select(ArticleORM)
            .where(ArticleORM.board_key == board_key)
            .where(ArticleORM.fetched_at >= cutoff)
        )
        rows = session.scalars(stmt).all()

        # Group inside the session — accessing ORM attributes after close
        # can trigger a refresh on a closed session (depending on expiry
        # state). We extract plain values here so callers don't have to
        # care about session lifetime.
        groups: dict[int, dict[str, Any]] = {}
        for r in rows:
            cid = r.cluster_id or r.id
            ts = r.fetched_at  # always set (default=utc_now in the model)
            tags = _decode_tags(r.tags)
            entry = groups.get(cid)
            if entry is None:
                groups[cid] = {
                    "title": r.title or "",
                    "url": r.url or "",
                    "source_label": r.source_label or "",
                    "rep_ts": ts,
                    "size": 1,
                    "sources": [r.source_label] if r.source_label else [],
                    "tag_set": set(tags),
                }
            else:
                entry["size"] += 1
                if r.source_label and r.source_label not in entry["sources"]:
                    entry["sources"].append(r.source_label)
                entry["tag_set"].update(tags)
                if ts > entry["rep_ts"]:
                    entry["title"] = r.title or ""
                    entry["url"] = r.url or ""
                    entry["source_label"] = r.source_label or ""
                    entry["rep_ts"] = ts

    clusters = [
        _Cluster(
            title=g["title"],
            url=g["url"],
            source_label=g["source_label"],
            rep_ts=g["rep_ts"],
            cluster_size=g["size"],
            sources=g["sources"],
            tags=sorted(g["tag_set"]),
        )
        for g in groups.values()
    ]
    clusters.sort(key=lambda c: (c.cluster_size, c.rep_ts), reverse=True)
    return clusters[:TOP_PER_BOARD]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_tag_chip_html(tag: str) -> str:
    return (
        f'<span style="display:inline-block;padding:1px 7px;margin:0 4px 0 0;'
        f'background:#eef;color:#334;border-radius:10px;font-size:11px;'
        f'line-height:1.6;">{html.escape(tag)}</span>'
    )


def _render_article_html(c: _Cluster) -> str:
    title = html.escape(c.title or "(untitled)")
    url = html.escape(c.url or "#", quote=True)
    source = html.escape(c.source_label or "")

    meta_parts: list[str] = []
    if source:
        meta_parts.append(f'<span style="color:#6b7280;">{source}</span>')
    if c.cluster_size > 1:
        meta_parts.append(
            f'<span style="display:inline-block;padding:1px 7px;'
            f'background:#fef3c7;color:#92400e;border-radius:10px;'
            f'font-size:11px;line-height:1.6;">{c.cluster_size} sources</span>'
        )
    if c.tags:
        meta_parts.append("".join(_render_tag_chip_html(t) for t in c.tags))

    sep = '<span style="color:#cbd5e1;margin:0 6px;">·</span>'
    meta_html = sep.join(meta_parts) if meta_parts else ""

    return (
        '<div style="padding:10px 0;border-bottom:1px solid #f1f5f9;">'
        f'<a href="{url}" style="color:#1f2937;text-decoration:none;'
        f'font-size:15px;font-weight:500;line-height:1.45;" target="_blank">'
        f'{title}</a>'
        f'<div style="margin-top:4px;font-size:12px;color:#6b7280;'
        f'line-height:1.6;">{meta_html}</div>'
        '</div>'
    )


def _render_board_section_html(board: Board, clusters: list[_Cluster]) -> str:
    color = _board_color(board.key)
    name = html.escape(board.name)
    count = len(clusters)

    header = (
        '<div style="margin:24px 0 8px 0;display:flex;align-items:center;">'
        f'<span style="display:inline-block;width:4px;height:18px;'
        f'background:{color};border-radius:2px;margin-right:10px;'
        f'vertical-align:middle;"></span>'
        f'<span style="font-size:18px;font-weight:600;color:#111827;'
        f'vertical-align:middle;">{name}</span>'
        f'<span style="font-size:13px;color:#6b7280;margin-left:8px;'
        f'vertical-align:middle;">({count} 篇)</span>'
        '</div>'
    )

    if not clusters:
        body = (
            '<div style="padding:14px 0;color:#9ca3af;font-size:13px;'
            'font-style:italic;">无新内容</div>'
        )
    else:
        body = "".join(_render_article_html(c) for c in clusters)

    return header + body


def _render_html(
    today_str: str,
    total: int,
    sections: list[tuple[Board, list[_Cluster]]],
    generated_at: str,
) -> str:
    sections_html = "".join(
        _render_board_section_html(board, clusters) for board, clusters in sections
    )

    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:24px 12px;'
        'background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',\'PingFang SC\',\'Hiragino Sans GB\',\'Microsoft YaHei\','
        'Arial,sans-serif;color:#111827;">'
        '<div style="max-width:680px;margin:0 auto;background:#ffffff;'
        'border-radius:12px;padding:28px 32px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
        # Header
        '<div style="border-bottom:2px solid #e5e7eb;padding-bottom:14px;'
        'margin-bottom:8px;">'
        '<div style="font-size:22px;font-weight:700;color:#111827;'
        'letter-spacing:0.3px;">TrendRadarCN 早报</div>'
        f'<div style="margin-top:4px;font-size:13px;color:#6b7280;">'
        f'{html.escape(today_str)} · 今日新增 <strong style="color:#111827;">'
        f'{total}</strong> 篇文章</div>'
        '</div>'
        f'{sections_html}'
        # Footer
        '<div style="margin-top:32px;padding-top:14px;'
        'border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;'
        'line-height:1.6;">'
        f'生成于 {html.escape(generated_at)} · '
        '<a href="http://127.0.0.1:8001" style="color:#9ca3af;'
        'text-decoration:underline;">本地仪表盘</a>'
        '</div>'
        '</div></body></html>'
    )


def _render_article_text(c: _Cluster) -> str:
    badges: list[str] = []
    if c.cluster_size > 1:
        badges.append(f"[{c.cluster_size} sources]")
    if c.tags:
        badges.append(f"[{','.join(c.tags)}]")
    badge_str = (" ".join(badges) + " ") if badges else ""

    title = (c.title or "(untitled)").strip()
    url = (c.url or "").strip()
    source = (c.source_label or "").strip()
    source_str = f"  ({source})" if source else ""

    return f"  • {badge_str}{title}{source_str}\n    {url}"


def _render_board_section_text(board: Board, clusters: list[_Cluster]) -> str:
    header = f"## {board.name} ({len(clusters)} 篇)"
    if not clusters:
        return f"{header}\n  (无新内容)"
    body = "\n".join(_render_article_text(c) for c in clusters)
    return f"{header}\n{body}"


def _render_text(
    today_str: str,
    total: int,
    sections: list[tuple[Board, list[_Cluster]]],
    generated_at: str,
) -> str:
    title = "TrendRadarCN 早报 · " + today_str
    bar = "=" * 40
    sections_text = "\n\n".join(
        _render_board_section_text(board, clusters) for board, clusters in sections
    )
    return (
        f"{title}\n{bar}\n今日新增 {total} 篇文章\n\n"
        f"{sections_text}\n\n"
        f"---\n生成于 {generated_at} · http://127.0.0.1:8001\n"
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_digest(hours: int = 24) -> dict[str, Any]:
    """Build the daily digest payload.

    Returns:
        {
          "subject": str,            # email subject line
          "html": str,               # full HTML body (inline CSS only)
          "text": str,               # plain-text fallback
          "total_articles": int,     # rendered cluster count across all boards
          "board_counts": {key: n},  # per-board rendered cluster count
        }
    """
    boards = load_boards()
    sections: list[tuple[Board, list[_Cluster]]] = []
    board_counts: dict[str, int] = {}
    total = 0
    for b in boards:
        clusters = _fetch_clusters(b.key, hours)
        sections.append((b, clusters))
        board_counts[b.key] = len(clusters)
        total += len(clusters)

    now_local = datetime.now().astimezone()
    today_str = now_local.strftime("%Y-%m-%d")
    generated_at = now_local.strftime("%Y-%m-%d %H:%M %Z").strip()

    subject = f"TrendRadarCN 早报 · {today_str}"
    html_body = _render_html(today_str, total, sections, generated_at)
    text_body = _render_text(today_str, total, sections, generated_at)

    return {
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "total_articles": total,
        "board_counts": board_counts,
    }

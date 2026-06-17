"""Build a daily HTML+text email digest of new board articles.

Usage:
    from app.digest import build_digest
    d = build_digest(hours=24)
    # d = {"subject": ..., "html": ..., "text": ..., "total_articles": int,
    #      "board_counts": {board_key: int, ...},
    #      "llm_cluster_stats": {board_key: {...}, ...}}

Two-stage event deduplication
-----------------------------
1. **Pre-clustering** (cheap, deterministic): each article already carries a
   ``cluster_id`` assigned at insert time by the char-bigram Jaccard logic in
   ``boards/dedup.py``. We group by ``cluster_id`` first, which collapses
   near-duplicate headlines (same wording across 3 outlets).
2. **LLM clustering** (semantic, optional): the pre-groups for each board are
   then sent to the LLM in one chat completion (``boards/llm_cluster``). The
   LLM merges groups that report the same *event* even when the wording
   differs ("小米发布 X 机器人" + "雷军谈 X 机器人售价" + "X 对标 Tesla" →
   one digest row). If the LLM is not configured or the call fails, the
   digest degrades cleanly to stage-1 only.

Per-board flow:
- Pull rows with ``fetched_at >= now - hours``.
- Pre-group by ``cluster_id`` (NULL → fall back to ``id``), up to
  ``_DIGEST_CANDIDATES`` (20) candidate clusters sorted by (size DESC,
  rep_ts DESC).
- Run LLM clustering on the candidate reps → merge → take top
  ``_DIGEST_PER_BOARD`` (8) final groups.
- Render both HTML (inline CSS — many email clients drop ``<style>``) and a
  plain-text fallback. Each final group shows "N sources: A · B · C" when
  it was merged from multiple outlets.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .boards import Board, load_boards
from .boards import llm_cluster
from .db import SessionLocal
from .models import Article as ArticleORM


# Candidate pool per board (stage-1 pre-groups handed to the LLM). 20 keeps
# the prompt small while still giving the LLM room to find non-obvious
# duplicates across outlets.
_DIGEST_CANDIDATES = 20

# Final rendered group count per board. Anything above 8 turns the digest
# into a wall of text that nobody reads on mobile.
_DIGEST_PER_BOARD = 8

# Backwards-compat alias — kept because external callers / tests sometimes
# import it. New code should reference ``_DIGEST_PER_BOARD``.
TOP_PER_BOARD = _DIGEST_PER_BOARD


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
    """One row in the rendered digest: a representative article plus
    aggregate metadata across all articles merged into this cluster
    (size, sources, tags, summary).

    Holds plain values (not the ORM object) so rendering can happen after
    the SQLAlchemy session has closed.
    """

    # Stable id used to round-trip through the LLM clustering call. We use
    # the representative article's DB primary key, which is unique within
    # the digest run.
    rep_id: int
    title: str
    url: str
    source_label: str
    rep_ts: datetime
    cluster_size: int
    sources: list[str]
    tags: list[str]
    # Prefer LLM-generated 1-sentence summary over the (often noisy) feed
    # summary. Empty string means "no usable summary" — renderer skips the line.
    summary: str = ""


def _fetch_pre_clusters(
    board_key: str, hours: int
) -> tuple[list[_Cluster], bool]:
    """Stage-1: pull recent articles for a board and pre-group.

    Grouping key precedence: ``llm_cluster_id`` > ``cluster_id`` > ``id``.
    When every row already has an ``llm_cluster_id`` set (the live path
    persists it via ``service.apply_llm_clustering``), we report
    ``persisted_llm=True`` so the caller can skip the runtime LLM round-trip.

    Returns up to ``_DIGEST_CANDIDATES`` clusters, sorted by
    (size DESC, rep_ts DESC).
    """
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
        all_have_llm = bool(rows)
        for r in rows:
            if r.llm_cluster_id:
                cid = r.llm_cluster_id
            else:
                all_have_llm = False
                cid = r.cluster_id or r.id
            ts = r.fetched_at  # always set (default=utc_now in the model)
            tags = _decode_tags(r.tags)
            summary_text = (r.llm_summary or r.summary or "").strip()
            entry = groups.get(cid)
            if entry is None:
                groups[cid] = {
                    "rep_id": r.id,
                    "title": r.title or "",
                    "url": r.url or "",
                    "source_label": r.source_label or "",
                    "rep_ts": ts,
                    "size": 1,
                    "sources": [r.source_label] if r.source_label else [],
                    "tag_set": set(tags),
                    "summary": summary_text,
                }
            else:
                entry["size"] += 1
                if r.source_label and r.source_label not in entry["sources"]:
                    entry["sources"].append(r.source_label)
                entry["tag_set"].update(tags)
                if ts > entry["rep_ts"]:
                    entry["rep_id"] = r.id
                    entry["title"] = r.title or ""
                    entry["url"] = r.url or ""
                    entry["source_label"] = r.source_label or ""
                    entry["rep_ts"] = ts
                    entry["summary"] = summary_text

    clusters = [
        _Cluster(
            rep_id=g["rep_id"],
            title=g["title"],
            url=g["url"],
            source_label=g["source_label"],
            rep_ts=g["rep_ts"],
            cluster_size=g["size"],
            sources=g["sources"],
            tags=sorted(g["tag_set"]),
            summary=g["summary"],
        )
        for g in groups.values()
    ]
    clusters.sort(key=lambda c: (c.cluster_size, c.rep_ts), reverse=True)
    return clusters[:_DIGEST_CANDIDATES], all_have_llm


def _merge_clusters(group: list[_Cluster]) -> _Cluster:
    """Merge several pre-clusters into one final cluster.

    Rep selection: the pre-cluster with the highest ``cluster_size`` wins.
    Ties are broken by ``rep_ts`` (most recent first). Sources and tags
    are unioned (preserving first-seen order for sources), counts summed.
    """
    rep = max(group, key=lambda c: (c.cluster_size, c.rep_ts))
    sources: list[str] = []
    seen_sources: set[str] = set()
    tag_set: set[str] = set()
    total = 0
    for c in group:
        total += c.cluster_size
        tag_set.update(c.tags)
        for s in c.sources:
            if s and s not in seen_sources:
                sources.append(s)
                seen_sources.add(s)
    # If the rep's source_label isn't in the merged list for some odd reason
    # (e.g. it was empty), don't synthesize one.
    return _Cluster(
        rep_id=rep.rep_id,
        title=rep.title,
        url=rep.url,
        source_label=rep.source_label,
        rep_ts=rep.rep_ts,
        cluster_size=total,
        sources=sources,
        tags=sorted(tag_set),
        summary=rep.summary,
    )


def _apply_llm_groups(
    pre_clusters: list[_Cluster], id_to_group: dict[int, str]
) -> list[_Cluster]:
    """Merge pre-clusters using LLM-assigned group labels.

    Pre-clusters with the same label collapse into a single final cluster.
    Items missing from ``id_to_group`` (shouldn't happen with the solo
    fallback, but defend anyway) become their own singleton group.
    """
    buckets: dict[str, list[_Cluster]] = {}
    order: list[str] = []
    for c in pre_clusters:
        label = id_to_group.get(c.rep_id) or f"_solo_{c.rep_id}"
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(c)
    return [_merge_clusters(buckets[label]) for label in order]


@dataclass
class _BoardResult:
    """Per-board digest output (rendered clusters + LLM stats for logging)."""

    clusters: list[_Cluster]
    llm_stats: dict[str, Any] = field(default_factory=dict)


async def _build_board_async(board: Board, hours: int) -> _BoardResult:
    """Compose stage-1 + stage-2 clustering for one board.

    Fast path: when every recent row already has ``llm_cluster_id`` set (the
    live ``--board`` path persists it via
    ``boards.service.apply_llm_clustering``), the pre-cluster step has already
    folded LLM-merged groups together and we skip the runtime LLM round-trip
    entirely. Stats report ``used_llm=False, persisted=True`` so the digest
    log makes the source of the merge clear.

    Slow path / fallback: when at least one row is missing ``llm_cluster_id``
    (legacy data, board never ran ``--board``, etc.), we still run the LLM
    over the pre-clusters at digest time so the email never regresses.
    """
    pre_clusters, persisted = _fetch_pre_clusters(board.key, hours)
    stats: dict[str, Any] = {"used_llm": False, "n_input": len(pre_clusters)}

    if persisted and pre_clusters:
        # llm_cluster_id has already done the merge in _fetch_pre_clusters.
        merged = pre_clusters
        stats["persisted"] = True
        stats["n_groups"] = len(pre_clusters)
    elif len(pre_clusters) > 1 and llm_cluster.is_configured():
        try:
            items = [
                {"id": c.rep_id, "title": c.title, "summary": c.summary}
                for c in pre_clusters
            ]
            result = await llm_cluster.cluster_articles(items)
            id_to_group = result.get("groups") or {}
            stats = {
                "used_llm": True,
                **(result.get("stats") or {}),
            }
            merged = _apply_llm_groups(pre_clusters, id_to_group)
        except Exception as exc:  # noqa: BLE001 - degrade silently to no-LLM grouping
            stats = {
                "used_llm": False,
                "n_input": len(pre_clusters),
                "error": f"{type(exc).__name__}: {exc}",
            }
            merged = pre_clusters
    else:
        merged = pre_clusters
        stats["n_groups"] = len(pre_clusters)

    merged.sort(key=lambda c: (c.cluster_size, c.rep_ts), reverse=True)
    return _BoardResult(clusters=merged[:_DIGEST_PER_BOARD], llm_stats=stats)


def _build_board(board: Board, hours: int) -> _BoardResult:
    """Sync wrapper for ``_build_board_async`` — convenient for the
    sync ``build_digest`` entrypoint and the CLI preview.
    """
    return asyncio.run(_build_board_async(board, hours))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_tag_chip_html(tag: str) -> str:
    return (
        f'<span style="display:inline-block;padding:1px 7px;margin:0 4px 0 0;'
        f'background:#eef;color:#334;border-radius:10px;font-size:11px;'
        f'line-height:1.6;">{html.escape(tag)}</span>'
    )


def _format_source_line(c: _Cluster) -> str:
    """Plain-text source line for a cluster.

    Cluster with 1 source → ``"36kr"``
    Cluster with N>1 sources → ``"3 sources: 36kr · 钛媒体 · 量子位"``
    """
    if c.cluster_size > 1 and len(c.sources) > 1:
        names = " · ".join(c.sources)
        return f"{c.cluster_size} sources: {names}"
    if c.sources:
        return c.sources[0]
    return c.source_label or ""


def _render_article_html(c: _Cluster) -> str:
    title = html.escape(c.title or "(untitled)")
    url = html.escape(c.url or "#", quote=True)

    meta_parts: list[str] = []
    # Source line: single name or "N sources: A · B · C".
    src_line = _format_source_line(c)
    if src_line:
        meta_parts.append(
            f'<span style="color:#6b7280;">{html.escape(src_line)}</span>'
        )
    # Keep the cluster badge for visual scan-ability (mobile + colorblind);
    # the source line says the same thing in words.
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

    summary_html = ""
    if c.summary:
        summary_html = (
            f'<div style="margin-top:4px;font-size:13px;color:#4b5563;'
            f'line-height:1.55;">{html.escape(c.summary)}</div>'
        )

    return (
        '<div style="padding:10px 0;border-bottom:1px solid #f1f5f9;">'
        f'<a href="{url}" style="color:#1f2937;text-decoration:none;'
        f'font-size:15px;font-weight:500;line-height:1.45;" target="_blank">'
        f'{title}</a>'
        f'{summary_html}'
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
        f'<a href="{html.escape(_dashboard_url())}" style="color:#9ca3af;'
        'text-decoration:underline;">仪表盘</a>'
        '</div>'
        '</div></body></html>'
    )


def _dashboard_url() -> str:
    """Resolve the URL printed in the email footer.

    Priority:
    1. ``TRENDRADAR_PUBLIC_URL`` — public HTTPS URL (e.g. GitHub Pages). Best
       for phones / external networks; click works in any mail client (including
       126 / Gmail webmail). This is the recommended setup once you've enabled
       Pages and run ``python run.py --snapshot-publish`` at least once.
    2. ``TRENDRADAR_DASHBOARD_URL`` — LAN URL (e.g. ``http://192.168.1.100:8001``).
       Works only when the reader is on the same LAN as the host.
    3. Auto-detected LAN IP via the standard UDP-socket trick (no packet
       actually sent; just asks the OS which interface would be used to reach
       8.8.8.8). Works on Windows / Linux / macOS without admin rights.
    4. Fall back to ``http://127.0.0.1:<port>`` if even autodetect fails
       (e.g., no network). The link will be a dead-end on other devices, but
       at least it points somewhere on the host that runs the server.

    Port comes from ``TRENDRADAR_PORT`` (default 8001) so it stays in sync
    with ``run.py``.
    """
    public = (os.getenv("TRENDRADAR_PUBLIC_URL") or "").strip()
    if public:
        return public.rstrip("/")

    explicit = (os.getenv("TRENDRADAR_DASHBOARD_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    port = (os.getenv("TRENDRADAR_PORT") or "8001").strip() or "8001"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
        if lan_ip and not lan_ip.startswith("127.") and not lan_ip.startswith("169.254."):
            return f"http://{lan_ip}:{port}"
    except OSError:
        pass

    return f"http://127.0.0.1:{port}"


def _render_article_text(c: _Cluster) -> str:
    badges: list[str] = []
    if c.cluster_size > 1:
        badges.append(f"[{c.cluster_size} sources]")
    if c.tags:
        badges.append(f"[{','.join(c.tags)}]")
    badge_str = (" ".join(badges) + " ") if badges else ""

    title = (c.title or "(untitled)").strip()
    url = (c.url or "").strip()
    src_line = _format_source_line(c)
    source_str = f"  ({src_line})" if src_line else ""

    out = f"  • {badge_str}{title}{source_str}"
    if c.summary:
        out += f"\n    {c.summary}"
    out += f"\n    {url}"
    return out


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
        f"---\n生成于 {generated_at} · {_dashboard_url()}\n"
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
          "llm_cluster_stats": {key: stats, ...},  # per-board LLM round-trip info
        }
    """
    boards = load_boards()
    sections: list[tuple[Board, list[_Cluster]]] = []
    board_counts: dict[str, int] = {}
    llm_stats: dict[str, dict[str, Any]] = {}
    total = 0
    for b in boards:
        result = _build_board(b, hours)
        sections.append((b, result.clusters))
        board_counts[b.key] = len(result.clusters)
        llm_stats[b.key] = result.llm_stats
        total += len(result.clusters)

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
        "llm_cluster_stats": llm_stats,
    }

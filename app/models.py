"""ORM models for TrendRadarCN."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = "sources"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(512))

    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(
        ForeignKey("sources.key"), index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="ok")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source: Mapped[Source] = relationship(back_populates="snapshots")
    topics: Mapped[list["Topic"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), index=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    snapshot: Mapped[Snapshot] = relationship(back_populates="topics")


# ---------------------------------------------------------------------------
# Topic boards: a parallel data path for "what's new on topic X today?"
# style use cases (financial news, industry digests, ...) — independent from
# the Source/Snapshot/Topic ranking path above.
# ---------------------------------------------------------------------------


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("board_key", "url", name="uq_article_board_url"),
        UniqueConstraint("board_key", "title", name="uq_article_board_title"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_key: Mapped[str] = mapped_column(String(64), index=True)
    source_label: Mapped[str] = mapped_column(String(128))  # e.g. "google_news", "eastmoney"
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSON-encoded list[str] of tag names (e.g. ["A股","财报"]). NULL = un-tagged.
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Event-cluster id. Articles with the same cluster_id are merged in the UI
    # (they cover the same event, reported by different sources). The
    # representative article's own id is reused as the cluster_id of its
    # cluster, so a singleton article has cluster_id == id.
    cluster_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Like cluster_id but assigned by LLM semantic clustering (see
    # boards/llm_cluster.py). When set, takes priority over cluster_id for
    # grouping in both digest and dashboard. The MIN article.id of a group is
    # reused as the cluster value, so it's stable across reruns.
    llm_cluster_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # LLM-generated 1-sentence summary, populated when the board has
    # ``summarizer: llm`` configured. NULL when not yet summarized or when
    # the board doesn't use the summarizer. We keep ``summary`` (the original
    # snippet from the feed) untouched and read ``llm_summary or summary``
    # at render time.
    llm_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BoardRun(Base):
    __tablename__ = "board_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_key: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="ok")
    feeds_total: Mapped[int] = mapped_column(Integer, default=0)
    feeds_ok: Mapped[int] = mapped_column(Integer, default=0)
    articles_seen: Mapped[int] = mapped_column(Integer, default=0)
    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

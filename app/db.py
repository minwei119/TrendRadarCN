"""SQLite database setup using SQLAlchemy."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = Path(__file__).resolve().parent.parent / "trendradar_cn.db"
ENGINE = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _ensure_columns() -> None:
    """Lightweight migration: add columns to existing tables when the
    model has grown new ones. Only handles ADD COLUMN (SQLite supports it
    without copying), which is enough for our schema evolution so far.
    """
    insp = inspect(ENGINE)
    table_specs = {
        "articles": [
            ("tags", "TEXT"),
            ("cluster_id", "INTEGER"),
            ("llm_summary", "TEXT"),
            ("llm_cluster_id", "INTEGER"),
        ],
    }
    with ENGINE.begin() as conn:
        for table, cols in table_specs.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, sql_type in cols:
                if name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def init_db() -> None:
    """Create tables and seed source registry."""
    from . import models  # noqa: F401 - register tables
    from .crawlers import iter_crawlers

    Base.metadata.create_all(ENGINE)
    _ensure_columns()

    with SessionLocal() as session:
        existing = {s.key for s in session.query(models.Source).all()}
        for crawler in iter_crawlers():
            if crawler.key in existing:
                continue
            session.add(
                models.Source(
                    key=crawler.key,
                    display_name=crawler.display_name,
                    region=crawler.region,
                    url=crawler.url,
                )
            )
        session.commit()


def get_session() -> Session:
    return SessionLocal()

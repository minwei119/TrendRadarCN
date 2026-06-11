"""FastAPI application for TrendRadarCN."""
from __future__ import annotations

from pathlib import Path

# Best-effort .env load so `uvicorn app.main:app` works without run.py.
# When started via run.py this is a no-op (already loaded). Must happen
# before any sibling import that reads os.environ at module import time.
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .boards import load_boards
from .boards.service import (
    backfill_tags_and_clusters,
    list_articles,
    list_boards_summary,
    recent_board_runs,
    run_all_boards,
    run_board,
)
from .db import init_db
from .obs import read_recent
from .service import (
    aggregate_top,
    crawl_all,
    crawl_one,
    latest_topics,
    latest_topics_all,
    list_sources,
    recent_snapshots,
    topic_trend,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="TrendRadarCN", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    # Backfill tags & clusters for any legacy rows. Idempotent + cheap when
    # there's nothing to do, so safe on every startup. For LLM-tagger boards
    # this may issue a few API calls on first start.
    try:
        await backfill_tags_and_clusters()
    except Exception as exc:  # noqa: BLE001 - don't block boot on a soft-fix
        print(f"[startup] backfill skipped: {exc}", flush=True)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sources")
async def api_sources() -> list[dict]:
    return list_sources()


@app.get("/api/hot")
async def api_hot(
    source: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict | list[dict]:
    if source:
        result = latest_topics(source, limit=limit)
        if not result:
            raise HTTPException(status_code=404, detail="no snapshot yet for this source")
        return result
    return latest_topics_all(limit_per_source=limit)


@app.get("/api/aggregate")
async def api_aggregate(
    hours: int = Query(default=24, ge=1, le=24 * 14),
    limit: int = Query(default=30, ge=1, le=200),
) -> list[dict]:
    return aggregate_top(hours=hours, limit=limit)


@app.get("/api/trend")
async def api_trend(
    title: str = Query(..., min_length=1),
    source: str | None = Query(default=None),
    hours: int = Query(default=48, ge=1, le=24 * 14),
) -> list[dict]:
    return topic_trend(title=title, source_key=source, hours=hours)


@app.get("/api/snapshots")
async def api_snapshots(
    source: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
) -> list[dict]:
    return recent_snapshots(source_key=source, limit=limit)


@app.get("/api/logs")
async def api_logs(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return read_recent(limit=limit)


@app.post("/api/crawl")
async def api_crawl(source: str | None = Query(default=None)) -> dict | list[dict]:
    if source:
        result = await crawl_one(source)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown source: {source}")
        return result
    return await crawl_all()


# --- Topic boards ----------------------------------------------------------


@app.get("/api/boards")
async def api_boards() -> list[dict]:
    return list_boards_summary()


@app.get("/api/boards/{board_key}/articles")
async def api_board_articles(
    board_key: str,
    hours: int = Query(default=48, ge=1, le=24 * 30),
    limit: int = Query(default=80, ge=1, le=500),
    tag: str | None = Query(default=None, min_length=1, max_length=64),
) -> list[dict]:
    if not any(b.key == board_key for b in load_boards()):
        raise HTTPException(status_code=404, detail=f"unknown board: {board_key}")
    return list_articles(board_key, hours=hours, limit=limit, tag=tag)


@app.get("/api/boards/{board_key}/runs")
async def api_board_runs(board_key: str, limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    return recent_board_runs(board_key=board_key, limit=limit)


@app.post("/api/boards/{board_key}/run")
async def api_board_run(board_key: str) -> dict:
    if not any(b.key == board_key for b in load_boards()):
        raise HTTPException(status_code=404, detail=f"unknown board: {board_key}")
    return await run_board(board_key)


@app.post("/api/boards/run")
async def api_boards_run_all() -> list[dict]:
    results: list[dict] = []
    async for r in run_all_boards():
        results.append(r)
    return results

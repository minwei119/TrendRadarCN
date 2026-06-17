"""Quick diagnostic: list articles matching keywords across all boards.

Shows board, llm_cluster_id, title, source, fetched_at so we can tell whether
LLM clustering merged or split semantically-similar news.

Usage:
    python scripts/diag_keyword.py 腾讯 回购
    python scripts/diag_keyword.py 周靖人
    python scripts/diag_keyword.py 阿里 --hours 24
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import and_, or_, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Article  # noqa: E402


def _safe(s) -> str:
    if s is None:
        return "<None>"
    try:
        return str(s)
    except Exception as e:
        return f"<unprintable: {type(e).__name__}>"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("keywords", nargs="+", help="ALL keywords must appear in title or summary")
    parser.add_argument("--hours", type=int, default=72)
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    conds = [Article.fetched_at >= cutoff]
    for kw in args.keywords:
        conds.append(or_(Article.title.contains(kw), Article.summary.contains(kw)))

    with SessionLocal() as db:
        rows = db.execute(
            select(Article).where(and_(*conds)).order_by(
                Article.board_key, Article.llm_cluster_id, Article.id
            )
        ).scalars().all()

    if not rows:
        print(f"No articles match {args.keywords} in last {args.hours}h.")
        return

    print(f"Matched {len(rows)} articles for {args.keywords} (last {args.hours}h):\n")

    last_key: tuple = (None, None)
    for r in rows:
        ts = r.fetched_at.strftime("%m-%d %H:%M") if r.fetched_at else "??"
        lcid_str = _safe(r.llm_cluster_id) if r.llm_cluster_id is not None else "NULL"
        cid_str = _safe(r.cluster_id) if r.cluster_id is not None else "NULL"
        cur_key = (r.board_key, r.llm_cluster_id)
        if cur_key != last_key:
            print(f"--- board={r.board_key}  llm_cluster_id={lcid_str}  cluster_id={cid_str}")
            last_key = cur_key
        title = _safe(r.title)[:120]
        src = _safe(r.source_label)
        summary = _safe(r.llm_summary or r.summary)[:160]
        print(f"    {title}")
        print(f"        src={src}  fetched={ts}  id={r.id}")
        if summary and summary != "<None>":
            print(f"        => {summary}")
    print()

    by_board_lcid: dict[tuple, int] = {}
    for r in rows:
        key = (r.board_key, r.llm_cluster_id if r.llm_cluster_id is not None else "NULL")
        by_board_lcid[key] = by_board_lcid.get(key, 0) + 1
    print("Group counts (board, llm_cluster_id) -> N articles:")
    for (board, lcid), n in sorted(by_board_lcid.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        print(f"  ({board}, lcid={lcid}) -> {n}")


if __name__ == "__main__":
    main()

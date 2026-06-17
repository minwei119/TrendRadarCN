"""Show article count per source_label per board (last N hours).

Useful for spotting source imbalance — e.g. GNews dominating, direct sources
underutilised, feeds returning zero items.

Usage:
    python scripts/diag_source_breakdown.py
    python scripts/diag_source_breakdown.py --hours 24
    python scripts/diag_source_breakdown.py --board my-portfolio
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import and_, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Article  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--board", help="Limit to one board (default: all)")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    conds = [Article.fetched_at >= cutoff]
    if args.board:
        conds.append(Article.board_key == args.board)

    with SessionLocal() as db:
        rows = db.execute(select(Article).where(and_(*conds))).scalars().all()

    by_board: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_board[r.board_key][r.source_label or "<unknown>"] += 1

    print(f"Article source breakdown (last {args.hours}h)\n")
    print(f"{'BOARD':14s} {'SOURCE':38s} {'COUNT':>6s} {'%':>6s}")
    print("-" * 70)

    for board_key in sorted(by_board.keys()):
        sources = by_board[board_key]
        total = sum(sources.values())
        print(f"\n[{board_key}]  total={total}")
        rows_sorted = sorted(sources.items(), key=lambda kv: -kv[1])
        gnews_total = 0
        for src, count in rows_sorted:
            pct = (count * 100.0 / total) if total else 0
            marker = " ★" if src.startswith("GNews") else ""
            print(f"{'':14s} {src:38s} {count:>6d} {pct:>5.1f}%{marker}")
            if src.startswith("GNews"):
                gnews_total += count
        gnews_pct = (gnews_total * 100.0 / total) if total else 0
        print(f"{'':14s} {'  → GNews 小计':38s} {gnews_total:>6d} {gnews_pct:>5.1f}%")


if __name__ == "__main__":
    main()

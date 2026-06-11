"""Event-level deduplication / clustering for board articles.

Why not URL-based dedup? URL dedup (already in place via the unique index on
board_key+url) only catches *exact* same article. But the same event is often
reported by 3+ outlets with different URLs and slightly different titles:

    华尔街见闻: "央行下调存款准备金率 0.5 个百分点"
    头条:        "重磅！央行降准 0.5 个百分点 释放万亿流动性"
    微博热搜:    "央行降准 0.5%"

These should be one row in the UI ("3 家同时报道"), not three.

Approach: character-bigram Jaccard similarity over normalized titles.
Cheap (no deps, no segmentation), language-agnostic, threshold-tunable, and
debuggable. Good enough for a personal news aggregator at this scale.
"""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize(title: str) -> str:
    """Strip whitespace and punctuation; keep letters/digits/CJK chars."""
    if not title:
        return ""
    return _PUNCT_RE.sub("", title).lower()


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def title_similarity(a: str, b: str) -> float:
    """Char-bigram Jaccard similarity in [0, 1].

    Works equally for CN and EN titles. For very short titles (1 char), we
    fall back to exact equality which is the correct degenerate behavior.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    A, B = _bigrams(na), _bigrams(nb)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    if inter == 0:
        return 0.0
    return inter / len(A | B)


def assign_cluster(
    new_title: str,
    existing: list[tuple[int, int, str]],
    threshold: float = 0.6,
) -> int | None:
    """Find the best matching cluster for ``new_title`` among ``existing``.

    Args:
      new_title: title of the article being placed.
      existing: list of ``(article_id, cluster_id, title)`` tuples from the
        candidate pool (typically: same board, last 48h, already persisted).
      threshold: minimum similarity to consider it the same event.

    Returns:
      The cluster_id to attach to the new article, or None if no close enough
      match was found (caller should then start a new cluster).
    """
    best_cid: int | None = None
    best_sim = threshold  # only accept matches that beat the threshold
    for _aid, cid, title in existing:
        sim = title_similarity(new_title, title)
        if sim > best_sim:
            best_sim = sim
            best_cid = cid
    return best_cid

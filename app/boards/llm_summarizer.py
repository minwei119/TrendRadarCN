"""LLM-based 1-sentence article summaries.

Per-board opt-in via ``summarizer: llm`` in boards.yaml. Talks to the same
OpenAI-compatible Chat Completions endpoint as ``llm_tagger.py`` (DeepSeek
by default) and reuses the same env vars. Output is stored in the new
``articles.llm_summary`` column; the original ``summary`` is left untouched.

Config (shared with llm_tagger):

  TRENDRADAR_LLM_API_KEY    mandatory; falls back to DEEPSEEK_API_KEY,
                            then OPENAI_API_KEY
  TRENDRADAR_LLM_BASE_URL   default: https://api.deepseek.com
  TRENDRADAR_LLM_MODEL      default: deepseek-chat

Summarizer-specific knobs (smaller defaults than the tagger because each
output sentence is heavier than a JSON tag list):

  TRENDRADAR_LLM_SUMMARY_BATCH        default 5
  TRENDRADAR_LLM_SUMMARY_CONCURRENCY  default 4

Articles are batched (one chat completion per batch returns a JSON array
for all items at once). Failed batches return ``None`` per item so the
caller can skip persistence and let the next backfill retry.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from ..config import llm_proxy
from ..obs import log_request


_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_BATCH = 5
_DEFAULT_CONCURRENCY = 4

# Hard cap on summary length after parsing — prevents the model from going
# off-script and writing a paragraph. ~80 CJK chars is roughly 25 words EN.
_MAX_SUMMARY_CHARS = 80

# Same DeepSeek-V3 chat pricing as llm_tagger.py — keep them in sync if you
# ever switch model/provider.
_PRICE_PER_M_TOKENS = {
    "input": 0.27,
    "output": 1.10,
}


def api_key() -> str | None:
    return (
        os.getenv("TRENDRADAR_LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )


def is_configured() -> bool:
    return bool(api_key())


def _base_url() -> str:
    return (os.getenv("TRENDRADAR_LLM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.getenv("TRENDRADAR_LLM_MODEL") or _DEFAULT_MODEL


def _batch_size() -> int:
    try:
        return max(1, int(os.getenv("TRENDRADAR_LLM_SUMMARY_BATCH", "")))
    except ValueError:
        return _DEFAULT_BATCH


def _concurrency() -> int:
    try:
        return max(1, int(os.getenv("TRENDRADAR_LLM_SUMMARY_CONCURRENCY", "")))
    except ValueError:
        return _DEFAULT_CONCURRENCY


_SYSTEM_PROMPT = """你是一个新闻摘要助手。
你会拿到一组新闻 (每条带标题和原始摘要)。
为每条新闻输出一句话总结, 严格 ONE SENTENCE, 不超过 60 字 (中文) 或 25 words (English)。
保持原文语言: 中文文章输出中文摘要, 英文文章输出英文摘要。
不要加 "据报道" / "本文称" / "this article says" 之类的引述前缀, 直接陈述事实。
不要使用 Markdown 格式 / 表情符号 / 引号。

输出严格的 JSON 数组, 每条对应一个输入 (顺序一致):
[
  {"id": 1, "summary": "..."},
  {"id": 2, "summary": "..."}
]
"""


def _build_user_prompt(batch: list[tuple[int, str, str]]) -> str:
    """Assemble the user message: 'N 条新闻, 各生成一句话摘要'."""
    n = len(batch)
    lines: list[str] = [f"请为以下 {n} 条新闻各生成一句话摘要:", ""]
    for idx, (art_id, title, summary) in enumerate(batch, start=1):
        summary_short = (summary or "").strip().replace("\n", " ")[:300]
        lines.append(f"{idx}) id: {art_id}")
        lines.append(f"   标题: {title}")
        if summary_short:
            lines.append(f"   原摘要: {summary_short}")
        else:
            lines.append("   原摘要: (无)")
        lines.append("")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """Some models wrap JSON in ```json ... ``` — peel it off if so."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    s = s.lstrip("`").lstrip()
    if s.lower().startswith("json"):
        s = s[4:].lstrip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s


# Defensive cleanup: strip leading attribution phrases the model loves to add
# despite the prompt, plus markdown wrappers and surrounding quotes.
_LEADING_NOISE = re.compile(
    r"^\s*(?:"
    r"据报道[:：,，]?\s*"
    r"|据悉[:：,，]?\s*"
    r"|本文称[:：,，]?\s*"
    r"|本文(?:报道|提到|介绍)[:：,，]?\s*"
    r"|文章称[:：,，]?\s*"
    r"|this\s+article\s+(?:says?|reports?|claims?|states?)[:,]?\s*"
    r"|the\s+article\s+(?:says?|reports?|claims?|states?)[:,]?\s*"
    r"|reportedly[:,]?\s*"
    r")",
    re.IGNORECASE,
)


def _clean_summary(text: str) -> str:
    s = (text or "").strip()
    # Strip surrounding quotes / markdown emphasis.
    s = s.strip("`*_")
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("“") and s.endswith("”")):
        s = s[1:-1].strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith("‘") and s.endswith("’")):
        s = s[1:-1].strip()
    # Strip attribution prefixes (may chain, e.g. "据报道, 本文称").
    for _ in range(3):
        new = _LEADING_NOISE.sub("", s)
        if new == s:
            break
        s = new.strip()
    # Hard cap. We slice on character count; for CJK this matches the prompt
    # contract and for English ~80 chars ≈ ~15-20 words which is fine.
    if len(s) > _MAX_SUMMARY_CHARS:
        s = s[:_MAX_SUMMARY_CHARS].rstrip() + "…"
    return s


def _parse_response(content: str, batch: list[tuple[int, str, str]]) -> dict[int, str | None]:
    """Parse the model's JSON output, defensively. Items without a usable
    summary map to ``None`` so the caller skips persistence for them."""
    out: dict[int, str | None] = {aid: None for aid, _, _ in batch}
    raw = _strip_code_fence(content)
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return out

    # Accept either a top-level list, or {"results": [...]}, or {"summaries": [...]}.
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "summaries", "data", "items"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break

    # If the model returned a list of strings (no ids), positional-align with batch.
    if rows and all(isinstance(r, str) for r in rows):
        for (aid, _, _), s in zip(batch, rows):
            cleaned = _clean_summary(s)
            out[aid] = cleaned or None
        return out

    # First pass: try matching by id field.
    matched_any = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            aid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if aid not in out:
            continue
        s = row.get("summary") or row.get("text") or row.get("content")
        if not isinstance(s, str):
            continue
        cleaned = _clean_summary(s)
        out[aid] = cleaned or None
        matched_any = True

    # Fallback: if the model returned dict rows but renumbered the ids
    # (e.g. always 1..N regardless of what we asked), positional-align.
    # This only kicks in when nothing matched by id at all.
    if not matched_any and rows:
        dict_rows = [r for r in rows if isinstance(r, dict)]
        for (aid, _, _), row in zip(batch, dict_rows):
            s = row.get("summary") or row.get("text") or row.get("content")
            if not isinstance(s, str):
                continue
            cleaned = _clean_summary(s)
            out[aid] = cleaned or None
    return out


async def _summarize_batch(
    batch: list[tuple[int, str, str]],
    key: str,
) -> tuple[dict[int, str | None], dict[str, Any]]:
    """Send ONE chat completion for the batch. Returns (summary_map, stats)."""
    started = time.perf_counter()
    url = f"{_base_url()}/v1/chat/completions"
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(batch)},
        ],
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, proxy=llm_proxy()) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        cost_usd = (
            in_tok / 1_000_000 * _PRICE_PER_M_TOKENS["input"]
            + out_tok / 1_000_000 * _PRICE_PER_M_TOKENS["output"]
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log_request(
            source="llm-summarizer",
            method="POST",
            url=url,
            host="api",
            ok=True,
            status=resp.status_code,
            elapsed_ms=elapsed_ms,
            outcome=f"items={len(batch)} in={in_tok} out={out_tok} usd={cost_usd:.5f}",
        )
        stats = {
            "items": len(batch),
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "cost_usd": cost_usd,
            "elapsed_ms": elapsed_ms,
        }
        return _parse_response(content, batch), stats
    except Exception as exc:  # noqa: BLE001 - log + return None so caller can retry
        log_request(
            source="llm-summarizer",
            method="POST",
            url=url,
            host="api",
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        return ({aid: None for aid, _, _ in batch}, {"error": str(exc)})


async def summarize_articles(
    items: list[dict],
    *,
    concurrency: int | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Generate 1-sentence LLM summaries for a list of articles.

    Args:
      items: ``[{"id": int, "title": str, "summary": str}, ...]``. ``id``
        round-trips through the prompt; typically the DB primary key.
        Falls back to a synthetic 1..N if missing.
      concurrency: parallel requests cap (default: env or 4).
      batch_size: articles per chat completion (default: env or 5).

    Returns:
      ``{"summaries": [str | None, ...], "stats": {...}}`` where
      ``summaries`` is positionally aligned with ``items``. ``None``
      means the call failed for that item — caller should skip persistence
      so the next backfill run retries it.
    """
    summaries: list[str | None] = [None] * len(items)
    if not items:
        return {"summaries": summaries, "stats": {"items": 0, "batches": 0}}

    key = api_key()
    if not key:
        return {"summaries": summaries, "stats": {"items": 0, "batches": 0,
                                                  "error": "TRENDRADAR_LLM_API_KEY not set"}}

    bs = batch_size or _batch_size()
    cc = concurrency or _concurrency()

    # Build (id, title, summary) tuples. Prefer the caller-supplied id
    # (e.g. DB primary key) for round-tripping; fall back to a 1-indexed
    # synthetic id. We also keep a reverse id→position map so we can
    # restore positional alignment regardless of the order the model
    # returns rows in.
    indexed: list[tuple[int, str, str]] = []
    id_to_pos: dict[int, int] = {}
    for i, it in enumerate(items):
        try:
            art_id = int(it.get("id"))
        except (TypeError, ValueError):
            art_id = i + 1
        # Disambiguate collisions (caller passed dup ids) by falling back.
        if art_id in id_to_pos:
            art_id = -(i + 1)
        title = str(it.get("title") or "").strip()
        summ = str(it.get("summary") or "").strip()
        indexed.append((art_id, title, summ))
        id_to_pos[art_id] = i

    batches: list[list[tuple[int, str, str]]] = [
        indexed[i : i + bs] for i in range(0, len(indexed), bs)
    ]
    sem = asyncio.Semaphore(cc)

    async def _run(batch):
        async with sem:
            return await _summarize_batch(batch, key)

    results = await asyncio.gather(*(_run(b) for b in batches), return_exceptions=False)

    total_in = total_out = 0
    total_cost = 0.0
    total_items = 0
    error_messages: list[str] = []
    for sum_map, stats in results:
        for art_id, s in sum_map.items():
            pos = id_to_pos.get(art_id)
            if pos is not None and 0 <= pos < len(summaries):
                summaries[pos] = s
        if stats.get("error"):
            error_messages.append(str(stats["error"]))
            continue
        total_in += int(stats.get("prompt_tokens", 0))
        total_out += int(stats.get("completion_tokens", 0))
        total_cost += float(stats.get("cost_usd", 0.0))
        total_items += int(stats.get("items", 0))

    summarized_n = sum(1 for s in summaries if s)
    return {
        "summaries": summaries,
        "stats": {
            "items": total_items,
            "batches": len(batches),
            "summarized": summarized_n,
            "prompt_tokens": total_in,
            "completion_tokens": total_out,
            "cost_usd": total_cost,
            "errors": len(error_messages),
            "error_messages": error_messages,
        },
    }


# Public aliases — keep symmetry with llm_tagger.py for any future --test-llm
# style helper.
base_url = _base_url
model = _model
SYSTEM_PROMPT = _SYSTEM_PROMPT
build_user_prompt = _build_user_prompt

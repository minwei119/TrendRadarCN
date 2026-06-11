"""LLM-based article classification.

Replaces rule-based tagging on a per-board basis (``tagger: llm`` in
boards.yaml). Talks to any OpenAI-compatible Chat Completions endpoint —
DeepSeek by default, but Moonshot/Kimi, 智谱 GLM, 通义, Ollama, OpenAI
itself all work by overriding the base URL / model.

Config is via env vars (no keys in the repo):

  TRENDRADAR_LLM_API_KEY    mandatory; falls back to DEEPSEEK_API_KEY,
                            then OPENAI_API_KEY
  TRENDRADAR_LLM_BASE_URL   default: https://api.deepseek.com
  TRENDRADAR_LLM_MODEL      default: deepseek-chat
  TRENDRADAR_LLM_BATCH      articles per request; default 10
  TRENDRADAR_LLM_CONCURRENCY parallel requests; default 4

Articles are batched (one chat completion per batch returns JSON for all
items at once). Failed batches return empty tags so the caller can leave
those rows NULL — they'll be retried on the next backfill run.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from ..obs import log_request


_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TIMEOUT = 45.0
_DEFAULT_BATCH = 10
_DEFAULT_CONCURRENCY = 4

# Price per 1M tokens, in USD — used purely for the runtime cost log so the
# user can see what each run costs. Update if you switch model/provider.
_PRICE_PER_M_TOKENS = {
    "input": 0.27,   # DeepSeek-V3 chat input
    "output": 1.10,  # DeepSeek-V3 chat output
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
        return max(1, int(os.getenv("TRENDRADAR_LLM_BATCH", "")))
    except ValueError:
        return _DEFAULT_BATCH


def _concurrency() -> int:
    try:
        return max(1, int(os.getenv("TRENDRADAR_LLM_CONCURRENCY", "")))
    except ValueError:
        return _DEFAULT_CONCURRENCY


_SYSTEM_PROMPT = """你是一个新闻分类助手。

你会拿到一组新闻 (每条带有 id、标题、可能还有摘要) 和一个候选标签列表。
任务: 给每条新闻打 0 个或多个标签 (可多选), 然后返回严格 JSON。

规则:
1. 只能使用候选标签列表里给出的标签名, 不要造新词
2. 没有合适标签的, tags 返回空数组 []
3. 标题里只提到公司名但不涉及该标签主题时, 不要打那个标签
   反例: "英伟达股价创新高" 不打 AI芯片 (这是公司股价新闻, 不是芯片技术)
   正例: "英伟达发布 H200, 推理性能提升 40%" 打 AI芯片 + 公司
4. 严格 JSON 格式, 形如:
   {"results":[{"id":1,"tags":["x","y"]}, {"id":2,"tags":[]}]}
"""


def _build_user_prompt(
    batch: list[tuple[int, str, str]],
    tag_hints: dict[str, list[str]],
) -> str:
    """Assemble the user message: candidate tags (with hint keywords) +
    the batch of articles."""
    lines: list[str] = ["候选标签 (标签名: 含义/示例关键词):"]
    for name, hints in tag_hints.items():
        # First ~6 keywords work as semantic hints. The tag NAME is the
        # only contract for the output.
        sample = ", ".join(str(h) for h in (hints or [])[:6])
        lines.append(f"- {name}" + (f"  (例如: {sample})" if sample else ""))
    lines.append("")
    lines.append("请给以下新闻分类, 按 id 返回:")
    lines.append("")
    for art_id, title, summary in batch:
        summary_short = (summary or "").strip().replace("\n", " ")[:200]
        lines.append(f"[{art_id}] 标题: {title}")
        if summary_short:
            lines.append(f"     摘要: {summary_short}")
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


def _parse_response(
    content: str, batch: list[tuple[int, str, str]], allowed: set[str]
) -> dict[int, list[str]]:
    """Parse the model's JSON output, defensively. Unknown tag names and
    out-of-batch ids are silently dropped."""
    out: dict[int, list[str]] = {aid: [] for aid, _, _ in batch}
    try:
        data = json.loads(_strip_code_fence(content))
    except (ValueError, json.JSONDecodeError):
        return out
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            aid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if aid not in out:
            continue
        tags = row.get("tags") or []
        if isinstance(tags, list):
            out[aid] = [
                str(t).strip()
                for t in tags
                if isinstance(t, (str, int)) and str(t).strip() in allowed
            ]
    return out


async def _classify_batch(
    batch: list[tuple[int, str, str]],
    tag_hints: dict[str, list[str]],
    key: str,
) -> tuple[dict[int, list[str]], dict[str, Any]]:
    """Send ONE chat completion for the batch. Returns (tag_map, stats)."""
    started = time.perf_counter()
    url = f"{_base_url()}/v1/chat/completions"
    allowed = set(tag_hints.keys())
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(batch, tag_hints)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
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
            source="llm-tagger",
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
        return _parse_response(content, batch, allowed), stats
    except Exception as exc:  # noqa: BLE001 - log + return empty so caller can retry
        log_request(
            source="llm-tagger",
            method="POST",
            url=url,
            host="api",
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        return ({aid: [] for aid, _, _ in batch}, {"error": str(exc)})


async def llm_tag_articles(
    articles: list[tuple[int, str, str]],
    tag_hints: dict[str, list[str]],
) -> tuple[dict[int, list[str]], dict[str, Any]]:
    """Tag a batch of articles using the configured LLM.

    Args:
      articles: ``[(article_id, title, summary), ...]``
      tag_hints: ``{tag_name: [keyword_or_example, ...]}`` — keys are the
        ONLY tags the LLM is allowed to output; values are passed as
        semantic hints in the prompt.

    Returns:
      (``{article_id: [tags]}``, ``aggregate_stats``).
      Article ids that failed get [] (caller leaves them un-tagged so
      they retry next backfill run).
    """
    key = api_key()
    if not key or not tag_hints or not articles:
        return ({aid: [] for aid, _, _ in articles}, {"items": 0})

    bs = _batch_size()
    cc = _concurrency()
    batches: list[list[tuple[int, str, str]]] = [
        articles[i : i + bs] for i in range(0, len(articles), bs)
    ]
    sem = asyncio.Semaphore(cc)

    async def _run(batch):
        async with sem:
            return await _classify_batch(batch, tag_hints, key)

    results = await asyncio.gather(*(_run(b) for b in batches), return_exceptions=False)

    merged: dict[int, list[str]] = {}
    total_in = total_out = 0
    total_cost = 0.0
    total_items = 0
    error_messages: list[str] = []
    for tag_map, stats in results:
        merged.update(tag_map)
        if stats.get("error"):
            error_messages.append(str(stats["error"]))
            continue
        total_in += int(stats.get("prompt_tokens", 0))
        total_out += int(stats.get("completion_tokens", 0))
        total_cost += float(stats.get("cost_usd", 0.0))
        total_items += int(stats.get("items", 0))

    return merged, {
        "items": total_items,
        "batches": len(batches),
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
        "cost_usd": total_cost,
        "errors": len(error_messages),
        "error_messages": error_messages,  # full list for debugging
    }


# Public aliases — used by the --test-llm CLI helper so it doesn't poke at
# underscore-prefixed names from another module.
base_url = _base_url
model = _model
SYSTEM_PROMPT = _SYSTEM_PROMPT
build_user_prompt = _build_user_prompt

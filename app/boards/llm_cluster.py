"""LLM-based semantic event clustering for the email digest.

The ingest-time dedup in ``dedup.py`` does cheap title-similarity clustering
(char-bigram Jaccard) which catches near-duplicate headlines. But the same
event reported by multiple outlets often has very different wording, e.g.::

    "小米发布 CyberOne 2 人形机器人"
    "雷军: 小米 CyberOne 售价 19.9 万"
    "小米机器人对标 Tesla Optimus"

All three are one event but Jaccard misses them. This module sends a small
batch of digest candidates to the LLM in one round-trip and lets it assign a
group label so the digest builder can collapse them into one row.

Talks to the same OpenAI-compatible Chat Completions endpoint as
``llm_tagger.py`` / ``llm_summarizer.py`` (DeepSeek by default) and reuses
the same env vars.

Config (shared with the other LLM modules):

  TRENDRADAR_LLM_API_KEY    mandatory; falls back to DEEPSEEK_API_KEY,
                            then OPENAI_API_KEY
  TRENDRADAR_LLM_BASE_URL   default: https://api.deepseek.com
  TRENDRADAR_LLM_MODEL      default: deepseek-chat

Single-batch design: we cluster up to ~20 articles per board per digest, so
one chat completion is enough. No batching / concurrency knobs.

On any failure (HTTP, JSON parse, missing rows) the function falls back to a
"no-op" result where each input id gets a unique group label, so the digest
builder degrades gracefully to the existing pre-LLM clustering.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from ..config import llm_proxy
from ..obs import log_request


_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TIMEOUT = 60.0

# Per-item summary cap in the prompt — keeps the request small. Titles are
# usually <60 chars; summaries can be 500+ in feed bodies. 150 chars per
# summary × 20 items ≈ 3 KB prompt, well within any model's context.
_MAX_SUMMARY_CHARS = 150

# Same DeepSeek-V3 chat pricing as the other LLM modules — keep in sync.
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


_SYSTEM_PROMPT = """你是新闻聚类助手。下面给你 N 条新闻 (id + 标题 + 摘要)。
请把"报道同一事件或同一焦点"的新闻合并为一组, 每组用字母代号 (A, B, C, ...)。

**用户偏好 (非常重要)**:
用户已明确说明: 邮件早报里同一焦点**只想看一条**, 不同媒体的不同视角他会自己去搜索。
所以你的任务是**激进地合并**, 把任何可能是同一焦点的新闻都归到一组。
**不确定时, 默认合并** (而不是分开)。漏聚的成本远高于误聚。

**应该合并的情况** (这些都是用户实际抱怨过的真实案例, 必须合并):
- 同公司 + 同产品/事件: "小米发布 X 机器人" + "雷军谈 X 售价" + "X 对标 Tesla" → 一组
- 同政策 + 同时点: "欧盟AI法案通过" + "解读欧盟AI法案" + "欧盟AI法案的影响" → 一组
- **同人物 + 近期言论或动态** (重点!): "阿里CTO周靖人在某会议演讲" + "周靖人解读AI战略" +
  "达摩院院长周靖人谈大模型" → 一组。即使标题写法、摘要角度完全不同, 只要主角是同一人、
  时间窗口在最近几天内, **默认按同事件合并**。
- **★ 用户实际抱怨案例 1**: "阿里巴巴 CTO 周靖人在某活动发言" + "阿里达摩院院长周靖人解读 AI 战略"
  + "多家媒体报道周靖人的同一发言 / 周靖人谈大模型 / 周靖人称 ..." → **必须一组**。
  同一人物 + 时间窗口在最近几天内 = 同一事件, 不要被不同的标题措辞 / 不同的报道角度迷惑。
- **★ 用户实际抱怨案例 2**: "腾讯回购股票公告" + "腾讯回购金额 X 亿" +
  "解读腾讯回购对股价的影响" + "腾讯连续 N 日回购" → **必须一组**。
  同一公司 + 同一公司动作 (回购), 即使标题、金额、时间维度的措辞完全不同, 也是**同一焦点**。
- 同公司同期重大动态被多家覆盖 (财报、收购、人事变动、产品更新、回购、分红) → 一组
- 同一论文 / 同一研究 / 同一开源项目 (作者或项目名相同) → 一组
- 同一会议、同一发布会、同一活动的多角度报道 → 一组

**应该保持分开**:
- 同一公司的明显不同事件: "OpenAI 发布 GPT-5" vs "OpenAI 起诉马斯克" → 两组
- 同一人物的不同主题: 某 CEO 谈业务 vs 谈个人生活 vs 不同时段的完全不同事件 → 多组
- 完全不同类别: 产品发布 vs 起诉 vs 招聘 vs 财报 → 分开

**判断流程**:
1. 看主角 (公司/人物/产品/政策) — 不同主角 → 分开
2. 主角相同 → 看事件类型/动作 — 类型明显不同 → 分开; 类型相同或模糊 → **合并**
3. 仍然犹豫 → **合并** (用户偏好优先)

**通过率自检 (非常重要!)**:
分组完成后回头数一下: 如果你最终的组数超过输入数的 70%, 说明你合并得**不够激进**, 请重新审视并合并更多。
理想压缩比是 50-70% (输入 20 条 → 输出 10-14 组)。如果输入 20 条出来 18 组, 这是**失败**的分组。
**典型表现**: 同公司同人物在同一个新闻周期里出现 2 条以上, 几乎一定漏合并了。

每条新闻必须分组, 单独的新闻独占一组。

输出严格的 JSON 数组, 顺序与输入一致:
[{"id": 1, "group": "A"}, {"id": 2, "group": "A"}, {"id": 3, "group": "B"}, ...]
"""


def _build_user_prompt(items: list[tuple[int, str, str]]) -> str:
    """Assemble the user message: 'N 条新闻, 请分组'."""
    n = len(items)
    lines: list[str] = [f"请给以下 {n} 条新闻分组:", ""]
    for idx, (art_id, title, summary) in enumerate(items, start=1):
        summary_short = (summary or "").strip().replace("\n", " ")[:_MAX_SUMMARY_CHARS]
        lines.append(f"{idx}) id: {art_id}")
        lines.append(f"   标题: {title}")
        if summary_short:
            lines.append(f"   摘要: {summary_short}")
        else:
            lines.append("   摘要: (无)")
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


def _solo_groups(items: list[tuple[int, str, str]]) -> dict[int, str]:
    """Fallback group assignment: every article is its own group.

    Used when the LLM call fails or the response is unparseable — caller's
    behavior degrades to "no LLM clustering" (the pre-LLM groups stay intact).
    """
    return {art_id: f"_solo_{art_id}" for art_id, _, _ in items}


def _parse_response(
    content: str, items: list[tuple[int, str, str]]
) -> dict[int, str]:
    """Parse the model's JSON output, defensively.

    Items without a usable group label fall back to their own singleton
    group (label ``_solo_<id>``), so a partial parse still degrades safely.
    """
    out: dict[int, str] = _solo_groups(items)
    raw = _strip_code_fence(content)
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return out

    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "groups", "data", "items"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break

    if not rows:
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
        g = row.get("group") or row.get("label") or row.get("cluster")
        if not isinstance(g, (str, int)):
            continue
        label = str(g).strip()
        if not label:
            continue
        out[aid] = label
        matched_any = True

    # Fallback: if the model renumbered ids (e.g. 1..N regardless of what we
    # asked) and nothing matched, positional-align by order in the request.
    if not matched_any:
        dict_rows = [r for r in rows if isinstance(r, dict)]
        for (aid, _, _), row in zip(items, dict_rows):
            g = row.get("group") or row.get("label") or row.get("cluster")
            if not isinstance(g, (str, int)):
                continue
            label = str(g).strip()
            if not label:
                continue
            out[aid] = label
    return out


async def cluster_articles(
    items: list[dict],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Cluster a small batch of articles by semantic event in ONE LLM call.

    Args:
      items: ``[{"id": int, "title": str, "summary": str}, ...]``.
        ``id`` round-trips through the prompt and identifies which articles
        belong to the same group.
      timeout: HTTP timeout for the chat completion (default 60s).

    Returns:
      ``{"groups": {id: group_label, ...}, "stats": {...}}``

      Group labels are opaque strings — typically ``"A"``, ``"B"`` from the
      LLM, or ``"_solo_<id>"`` for items that fell back to singletons. The
      caller should treat them as bucket keys, not display strings.

      On any failure (no API key, HTTP error, JSON parse failure) every item
      gets its own ``_solo_<id>`` group, which is equivalent to "no
      clustering happened" — the caller's behavior is unchanged.

      ``stats`` always includes the keys ``n_input`` and ``n_groups``. If the
      call succeeded it also includes ``prompt_tokens``, ``completion_tokens``,
      ``cost_usd``, ``elapsed_ms``. On failure it includes ``error``.
    """
    n_in = len(items)
    indexed: list[tuple[int, str, str]] = []
    seen_ids: set[int] = set()
    for i, it in enumerate(items):
        try:
            art_id = int(it.get("id"))
        except (TypeError, ValueError):
            art_id = i + 1
        # Disambiguate caller-supplied duplicate ids by falling back to a
        # negative synthetic id (still stable per position).
        if art_id in seen_ids:
            art_id = -(i + 1)
        title = str(it.get("title") or "").strip()
        summ = str(it.get("summary") or "").strip()
        indexed.append((art_id, title, summ))
        seen_ids.add(art_id)

    if not indexed:
        return {"groups": {}, "stats": {"n_input": 0, "n_groups": 0}}

    key = api_key()
    if not key:
        groups = _solo_groups(indexed)
        return {
            "groups": groups,
            "stats": {
                "n_input": n_in,
                "n_groups": len(set(groups.values())),
                "error": "TRENDRADAR_LLM_API_KEY not set",
            },
        }

    started = time.perf_counter()
    url = f"{_base_url()}/v1/chat/completions"
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(indexed)},
        ],
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=llm_proxy()) as client:
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
        groups = _parse_response(content, indexed)
        n_groups = len(set(groups.values()))
        log_request(
            source="llm-cluster",
            method="POST",
            url=url,
            host="api",
            ok=True,
            status=resp.status_code,
            elapsed_ms=elapsed_ms,
            outcome=(
                f"input={n_in} groups={n_groups} "
                f"in={in_tok} out={out_tok} usd={cost_usd:.5f}"
            ),
        )
        return {
            "groups": groups,
            "stats": {
                "n_input": n_in,
                "n_groups": n_groups,
                "prompt_tokens": in_tok,
                "completion_tokens": out_tok,
                "cost_usd": cost_usd,
                "elapsed_ms": elapsed_ms,
            },
        }
    except Exception as exc:  # noqa: BLE001 - log + return solo groups so caller degrades cleanly
        log_request(
            source="llm-cluster",
            method="POST",
            url=url,
            host="api",
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        groups = _solo_groups(indexed)
        return {
            "groups": groups,
            "stats": {
                "n_input": n_in,
                "n_groups": len(set(groups.values())),
                "error": f"{type(exc).__name__}: {exc}",
            },
        }


base_url = _base_url
model = _model
SYSTEM_PROMPT = _SYSTEM_PROMPT
build_user_prompt = _build_user_prompt

"""Static dashboard snapshot generator.

把当前数据库里的板块文章导出成一个**自包含**的静态 HTML 页面
(``docs/index.html``), 配合 GitHub Pages 即可零成本公网访问:

- LAN 方案 (``TRENDRADAR_DASHBOARD_URL``) 只在同一 WiFi 下生效;
- GitHub Pages 方案 (``TRENDRADAR_PUBLIC_URL``) 任意网络都能打开,
  最关键是邮件 (126 / Gmail / QQ / Outlook 网页版) 里点链接也能跳。

设计要点 (与 ``app/static/index.html`` 实时仪表盘相互独立):

- **单文件**: HTML / CSS (Tailwind CDN) / JS / 数据全部塞进一个 ``index.html``,
  下载完即可 ``file://`` 打开看完整效果, 不依赖 ``/api/`` 后端。
- **数据内联**: ``<script>const DATA = {...}</script>``, 不拆分 ``data.json``,
  避免 GitHub Pages 上拿 stale cache 之类的小坑, 部署只需 push 一个文件。
- **离线渲染**: 服务端把所有板块的 ``list_articles()`` 结果 (已含事件聚类 +
  LLM 摘要) 提前算好序列化, 客户端只剩搜索 / 过滤 / 切换 tab 的轻量逻辑。
- **状态在 URL hash**: ``#board=ai-cn&tags=模型,公司&q=英伟达`` 刷新后状态不丢,
  也方便把"当前视图"直接分享给别人。
- **零运行依赖**: 全部用标准库 (``json`` / ``pathlib`` / ``html``); Tailwind
  从 CDN 加载, 即使 CDN 挂了也只是没样式, 内容仍然可读。

Public API:
    build_snapshot(*, output_dir, per_board_limit, lookback_hours, public_url)
        生成 ``output_dir/index.html``, 返回 ``{path, boards, total, bytes}``。
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .boards import load_boards
from .boards.service import list_articles


# 与邮件早报里的板块色 (``app/digest.py`` 的 ``_BOARD_PALETTE``) 保持一致,
# 这样邮件 + 快照页 + 实时仪表盘三处颜色风格统一, 用户一眼对得上。
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


# 超过这个阈值就在 stdout 打 warn。Tailwind CDN 之后, 一个空模板大约 6KB;
# 5 个板 × 30 篇 (含 llm_summary) 实测多在 200KB 量级。1MB 是写得很啰嗦才
# 会触及的红线, 用来兜底防止哪天数据规模翻倍后无人察觉。
_SIZE_WARN_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# 数据收集
# ---------------------------------------------------------------------------


def _collect_board_articles(
    board_key: str,
    *,
    per_board_limit: int,
    lookback_hours: int,
) -> list[dict[str, Any]]:
    """从 ``list_articles`` 拿一个板块的 cluster-merged 文章列表, 再做一遍
    "快照专用" 的字段精简 (剔除前端不会用到的列, 减小 HTML 体积)。

    ``list_articles`` 已经完成: cluster_id 分组 → 选最新代表 →
    取并集 sources / tags → llm_summary 优先。我们直接复用, 避免重写
    分组逻辑造成实时仪表盘 vs 快照不一致。
    """
    try:
        rows = list_articles(
            board_key,
            hours=lookback_hours,
            limit=per_board_limit,
            tag=None,
        )
    except Exception as exc:  # noqa: BLE001 - 单个板崩了不能阻断整个快照
        print(
            f"[snapshot] WARN: list_articles({board_key}) failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        # title 必有; url 可能为空 (理论上不会, 但防御一手 — 没有 url 的
        # 文章渲染时会变成纯文本, 不让点击)。
        title = (r.get("title") or "").strip()
        if not title:
            continue
        url = (r.get("url") or "").strip()
        # llm_summary 优先于 feed summary; 都没有就空字符串 (前端会跳过这一行)。
        llm_summary = (r.get("llm_summary") or "").strip()
        raw_summary = (r.get("summary") or "").strip()
        summary_text = llm_summary or raw_summary

        sources = list(r.get("cluster_sources") or [])
        # 单源时, 第一个 source label 通常等于代表文章自己的 source —
        # 前端按 cluster_size > 1 来决定要不要展示 "N 源", 这里照搬即可。
        cluster_size = int(r.get("cluster_count") or 1)

        # 时间统一以 ISO 字符串落到前端, 让浏览器原生 ``new Date(...)`` 解析。
        # ``published_at`` 偶尔为 NULL (有些 feed 不给), 此时用 ``fetched_at``
        # 兜底, 这样卡片右下角 "N 小时前" 永远有值。
        ts_iso = r.get("published_at") or r.get("fetched_at") or ""

        out.append(
            {
                "id": r.get("id"),
                "title": title,
                "url": url,
                "source": r.get("source") or "",
                "sources": sources,
                "summary": summary_text,
                "tags": list(r.get("tags") or []),
                "cluster_size": cluster_size,
                "ts": ts_iso,
            }
        )
    return out


def _collect_all_boards(
    per_board_limit: int, lookback_hours: int
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """返回 ``(boards_payload, counts_by_key, total_articles)``。

    ``boards_payload`` 直接作为前端 ``DATA.boards`` 的内容,
    每一项形如::

        {
          "key": "ai-cn",
          "name": "AI 前沿中文",
          "color": "#7c3aed",
          "tags": ["模型", "公司", ...],
          "articles": [ {title, url, ...}, ... ],
        }
    """
    boards_payload: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    total = 0
    for board in load_boards():
        articles = _collect_board_articles(
            board.key,
            per_board_limit=per_board_limit,
            lookback_hours=lookback_hours,
        )
        # 配置里 ``board.tags`` 是 ``{name: [keywords...]}``; 前端只用 name 当
        # chip 文字, 所以这里只导出 key 列表。LLM tagger 给文章打的标签也是
        # 这些 name 中的一项, 所以两边是同一个 vocab。
        tag_names = list(board.tags.keys())
        boards_payload.append(
            {
                "key": board.key,
                "name": board.name,
                "description": board.description,
                "color": _board_color(board.key),
                "tags": tag_names,
                "articles": articles,
            }
        )
        counts[board.key] = len(articles)
        total += len(articles)
    return boards_payload, counts, total


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------


# Tailwind CDN script 加 ``defer`` 会让首屏样式闪一下; 直接放 ``<head>`` 同步
# 加载, 文档很短, 阻塞时间忽略不计 (主要瓶颈是 CDN 网络)。
_TAILWIND_CDN = (
    '<script src="https://cdn.tailwindcss.com"></script>'
)


def _safe_json(data: Any) -> str:
    """JSON.dumps + 防 ``</script>`` 注入 (静态 HTML 里也要稳)。

    - ``ensure_ascii=False`` 保留中文, 体积更小;
    - 任何 ``<`` 都转成 ``\\u003c``, 避免标题里出现 ``</script>`` 这种
      字符串提前关闭 script 块 (这是 OWASP 推荐的内联 JSON 写法);
    - 不加缩进 (压扁), 节省 30% 左右体积。
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render_html(
    *,
    boards_payload: list[dict[str, Any]],
    total: int,
    generated_at_local: str,
    generated_at_iso: str,
    lookback_hours: int,
    public_url: str | None,
) -> str:
    """组合最终 HTML。所有用户内容都走 JS 端的 ``escapeHtml``, 所以
    模板里不需要 ``html.escape`` 数据 — 只 escape 元信息文本。
    """
    days = max(1, lookback_hours // 24)
    title = "TrendRadarCN · 仪表盘"

    payload = {
        "generated_at": generated_at_iso,
        "generated_at_display": generated_at_local,
        "lookback_hours": lookback_hours,
        "total": total,
        "boards": boards_payload,
    }
    data_json = _safe_json(payload)

    canonical_tag = ""
    if public_url:
        canonical_tag = (
            f'<link rel="canonical" href="{html.escape(public_url, quote=True)}" />'
        )

    repo_link = "https://github.com/minwei119/TrendRadarCN"

    # NOTE: 单文件交付, 不拆 CSS / JS。JS 部分用普通 ``<script>`` 而不是
    # ``type="module"`` —— ``file://`` 直接打开时 module script 会被浏览器
    # 因 CORS 拦掉, 退回到 inline 是最稳的方案。
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>{html.escape(title)}</title>
<meta name="description" content="TrendRadarCN — 中文互联网热点 + 主题板块每日快照" />
{canonical_tag}
{_TAILWIND_CDN}
<style>
  html, body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC",
                  "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  /* iOS Safari: 隐藏横向滚动条 */
  .no-scrollbar::-webkit-scrollbar {{ display: none; }}
  .no-scrollbar {{ -ms-overflow-style: none; scrollbar-width: none; }}
  /* 标题最多 3 行省略 */
  .line-clamp-3 {{
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .line-clamp-2 {{
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  /* tap-highlight 关掉 (iOS 点卡片不要有蓝色闪) */
  a, button {{ -webkit-tap-highlight-color: transparent; }}
</style>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen">

<header class="sticky top-0 z-30 bg-white/90 backdrop-blur border-b border-slate-200">
  <div class="max-w-3xl mx-auto px-4 py-3">
    <div class="flex items-center gap-3">
      <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-rose-500 to-amber-400 flex items-center justify-center text-base font-bold text-white shadow-sm">热</div>
      <div class="flex-1 min-w-0">
        <h1 class="text-base font-semibold leading-tight">TrendRadarCN · 仪表盘</h1>
        <p class="text-[11px] text-slate-500 leading-tight mt-0.5">
          生成于 <span id="meta-generated"></span> · 共 <span id="meta-total"></span> 篇 · 数据范围 {days} 天
        </p>
      </div>
    </div>
    <div class="mt-2 text-[11px] text-slate-500 bg-amber-50 border border-amber-200 rounded-md px-2 py-1">
      📬 邮件每日 7:30 自动推送 · 此页同步刷新
    </div>
    <!-- board tabs (sticky, 横向滚动) -->
    <div class="mt-2 -mx-4 px-4 overflow-x-auto no-scrollbar">
      <div id="board-tabs" class="flex gap-1.5 pb-1 min-w-max"></div>
    </div>
  </div>
</header>

<main class="max-w-3xl mx-auto px-4 py-3 space-y-3">
  <!-- search -->
  <div>
    <input id="search" type="search"
      placeholder="搜索标题或摘要…"
      class="w-full px-3 py-2 text-sm bg-white border border-slate-200 rounded-lg
             placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-rose-200
             focus:border-rose-300" />
  </div>

  <!-- tag chips -->
  <div id="tag-bar" class="flex flex-wrap items-center gap-1.5"></div>

  <!-- meta line (counts after filter) -->
  <div id="active-meta" class="text-[11px] text-slate-500"></div>

  <!-- article list -->
  <div id="article-list" class="space-y-2"></div>

  <!-- empty state -->
  <div id="empty-state" class="hidden text-center text-sm text-slate-400 py-12">
    无匹配
  </div>
</main>

<footer class="max-w-3xl mx-auto px-4 py-8 text-center text-[11px] text-slate-400 leading-relaxed">
  TrendRadarCN · MIT · <a class="underline hover:text-slate-600" href="{html.escape(repo_link, quote=True)}" target="_blank" rel="noopener">github.com/minwei119/TrendRadarCN</a>
  <br/>
  生成时间 <span id="footer-generated"></span>
</footer>

<script>
const DATA = {data_json};

// Tag color palette — 和实时仪表盘 (app/static/index.html) 那套保持一致,
// 这样用户切来切去不会觉得标签颜色对不上号。
const TAG_PALETTE = [
  ["bg-emerald-50",  "text-emerald-700",  "border-emerald-200"],
  ["bg-sky-50",      "text-sky-700",      "border-sky-200"],
  ["bg-amber-50",    "text-amber-700",    "border-amber-200"],
  ["bg-fuchsia-50",  "text-fuchsia-700",  "border-fuchsia-200"],
  ["bg-violet-50",   "text-violet-700",   "border-violet-200"],
  ["bg-rose-50",     "text-rose-700",     "border-rose-200"],
  ["bg-cyan-50",     "text-cyan-700",     "border-cyan-200"],
  ["bg-lime-50",     "text-lime-700",     "border-lime-200"],
];
function tagClass(name) {{
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return TAG_PALETTE[Math.abs(h) % TAG_PALETTE.length].join(" ");
}}

function escapeHtml(s) {{
  return String(s ?? "").replace(/[&<>"']/g, c => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }}[c]));
}}

function fmtTime(iso) {{
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diffSec = (Date.now() - d.getTime()) / 1000;
  if (diffSec < 60) return `${{Math.max(1, Math.round(diffSec))}} 秒前`;
  if (diffSec < 3600) return `${{Math.round(diffSec / 60)}} 分钟前`;
  if (diffSec < 86400) return `${{Math.round(diffSec / 3600)}} 小时前`;
  if (diffSec < 86400 * 7) return `${{Math.round(diffSec / 86400)}} 天前`;
  return d.toLocaleDateString("zh-CN", {{ month: "2-digit", day: "2-digit" }});
}}

// ----- State (in URL hash) -----
// 用 hash 而不是 query string, 因为 GitHub Pages 重新发布时浏览器会
// 重新请求页面; hash 不参与请求, 不会让用户掉到 "?board=xxx" 上加载失败。
//
// state = {{ board: "all" | <key>, tags: Set<string>, q: string }}
const state = {{ board: "all", tags: new Set(), q: "" }};

function parseHash() {{
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const params = new URLSearchParams(raw);
  state.board = params.get("board") || "all";
  state.tags = new Set((params.get("tags") || "")
    .split(",").map(s => s.trim()).filter(Boolean));
  state.q = params.get("q") || "";
}}

function syncHash() {{
  const params = new URLSearchParams();
  if (state.board && state.board !== "all") params.set("board", state.board);
  if (state.tags.size) params.set("tags", Array.from(state.tags).join(","));
  if (state.q) params.set("q", state.q);
  const next = params.toString();
  // 用 replaceState 避免每次切换都污染 history 栈 (移动端体验更顺)
  if (next) {{
    history.replaceState(null, "", "#" + next);
  }} else {{
    history.replaceState(null, "", location.pathname + location.search);
  }}
}}

// ----- Pure helpers -----
function activeBoardKeys() {{
  if (state.board === "all") return DATA.boards.map(b => b.key);
  return [state.board];
}}

// 返回 [{{board, article}}, ...] (扁平化, 方便统一搜索/过滤)
function visibleArticles({{ ignoreTags = false }} = {{}}) {{
  const keys = new Set(activeBoardKeys());
  const q = state.q.trim().toLowerCase();
  const out = [];
  for (const b of DATA.boards) {{
    if (!keys.has(b.key)) continue;
    for (const a of b.articles) {{
      // tag filter (OR; ignore for tag-count recompute)
      if (!ignoreTags && state.tags.size) {{
        const at = a.tags || [];
        let hit = false;
        for (const t of at) {{ if (state.tags.has(t)) {{ hit = true; break; }} }}
        if (!hit) continue;
      }}
      if (q) {{
        const hay = (a.title + " " + (a.summary || "")).toLowerCase();
        if (!hay.includes(q)) continue;
      }}
      out.push({{ board: b, article: a }});
    }}
  }}
  // 按时间倒序 (跨板块混合时也要保证最新在前)
  out.sort((x, y) => (y.article.ts || "").localeCompare(x.article.ts || ""));
  return out;
}}

// ----- Renderers -----
function renderBoardTabs() {{
  const wrap = document.getElementById("board-tabs");
  const items = [{{ key: "all", name: "全部", color: "#475569",
                    count: DATA.boards.reduce((s, b) => s + b.articles.length, 0) }}];
  for (const b of DATA.boards) {{
    items.push({{ key: b.key, name: b.name, color: b.color, count: b.articles.length }});
  }}
  wrap.innerHTML = items.map(it => {{
    const active = (it.key === state.board);
    const cls = active
      ? "bg-slate-900 text-white border-slate-900"
      : "bg-white text-slate-700 border-slate-200 hover:border-slate-400";
    const dot = active
      ? ""
      : `<span class="inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle"
              style="background:${{it.color}}"></span>`;
    return `<button data-key="${{it.key}}"
       class="board-tab whitespace-nowrap px-3 py-1.5 text-xs rounded-full border ${{cls}}">
       ${{dot}}${{escapeHtml(it.name)}}
       <span class="ml-1 opacity-70">${{it.count}}</span>
    </button>`;
  }}).join("");
  wrap.querySelectorAll(".board-tab").forEach(el => {{
    el.addEventListener("click", () => {{
      if (state.board === el.dataset.key) return;
      state.board = el.dataset.key;
      state.tags.clear();  // 切板时清空 tag (每个板有自己的 tag vocab)
      syncHash(); renderAll();
    }});
  }});
}}

function tagsForActiveBoard() {{
  // 把当前可见板块下文章上**实际出现过**的 tag 全部收集; 这样 chip 显示的
  // 都是真的有内容的标签, 不会出现 "点了却 0 结果" 的死标签。
  const keys = new Set(activeBoardKeys());
  const set = new Set();
  // 单个板时, 也把 board 配置里的 tag vocab 加进来 (即使本次没文章命中);
  // 防御没文章时 chip bar 完全空白看着奇怪。
  if (state.board !== "all") {{
    const b = DATA.boards.find(x => x.key === state.board);
    if (b) for (const t of (b.tags || [])) set.add(t);
  }}
  for (const b of DATA.boards) {{
    if (!keys.has(b.key)) continue;
    for (const a of b.articles) for (const t of (a.tags || [])) set.add(t);
  }}
  return Array.from(set).sort();
}}

function renderTagBar() {{
  const bar = document.getElementById("tag-bar");
  const tags = tagsForActiveBoard();
  if (!tags.length) {{ bar.innerHTML = ""; return; }}

  // 标签计数: 复用 visibleArticles({{ignoreTags: true}}) 的结果 (仍然受
  // board / 搜索影响) 来重新算每个 tag 的命中数; 这样切板 / 改关键词时
  // 数字会跟着变, 跟谷歌图片 / 邮箱标签筛选体验一致。
  const pool = visibleArticles({{ ignoreTags: true }});
  const counts = new Map();
  for (const {{ article }} of pool) {{
    for (const t of (article.tags || [])) counts.set(t, (counts.get(t) || 0) + 1);
  }}
  const totalAll = pool.length;

  const allCls = state.tags.size === 0
    ? "bg-slate-900 text-white border-slate-900"
    : "bg-white text-slate-600 border-slate-200 hover:border-slate-400";
  const chips = [`<button data-tag="" class="tag-chip px-2 py-0.5 rounded-full text-[11px] border ${{allCls}}">全部 <span class="opacity-70">(${{totalAll}})</span></button>`];

  for (const t of tags) {{
    const active = state.tags.has(t);
    const base = tagClass(t);
    const cls = active
      ? `${{base}} ring-2 ring-offset-1 ring-slate-900`
      : `${{base}} hover:brightness-95`;
    const n = counts.get(t) || 0;
    chips.push(`<button data-tag="${{escapeHtml(t)}}"
      class="tag-chip px-2 py-0.5 rounded-full text-[11px] border ${{cls}}">
      ${{escapeHtml(t)}} <span class="opacity-70">(${{n}})</span>
    </button>`);
  }}
  bar.innerHTML = chips.join("");
  bar.querySelectorAll(".tag-chip").forEach(el => {{
    el.addEventListener("click", () => {{
      const t = el.dataset.tag;
      if (!t) {{ state.tags.clear(); }}
      else if (state.tags.has(t)) {{ state.tags.delete(t); }}
      else {{ state.tags.add(t); }}
      syncHash(); renderAll();
    }});
  }});
}}

function renderArticles() {{
  const list = document.getElementById("article-list");
  const empty = document.getElementById("empty-state");
  const meta = document.getElementById("active-meta");

  const rows = visibleArticles();
  if (!rows.length) {{
    list.innerHTML = "";
    empty.classList.remove("hidden");
    meta.textContent = "";
    return;
  }}
  empty.classList.add("hidden");

  const boardName = state.board === "all"
    ? "全部板块"
    : (DATA.boards.find(b => b.key === state.board) || {{name: state.board}}).name;
  const tagPart = state.tags.size
    ? ` · 标签 ${{Array.from(state.tags).map(escapeHtml).join(" / ")}}`
    : "";
  const qPart = state.q ? ` · 搜索 “${{escapeHtml(state.q)}}”` : "";
  meta.textContent = `${{boardName}} · ${{rows.length}} 条${{tagPart}}${{qPart}}`;

  list.innerHTML = rows.map(({{ board, article: a }}) => {{
    const url = a.url ? escapeHtml(a.url) : "";
    const isLink = !!a.url;

    // badges 排在标题前: 多源徽章 + 板块色点 + tag chips。tag chip 与
    // 主 tag bar 复用 tagClass(), 颜色一致。
    const badges = [];
    if (a.cluster_size > 1) {{
      badges.push(`<span class="inline-flex items-center px-1.5 py-0.5 rounded-md bg-amber-100 text-amber-800 text-[10px] font-medium">📰 ${{a.cluster_size}} 源</span>`);
    }}
    if (state.board === "all") {{
      badges.push(`<span class="inline-flex items-center gap-1 text-[10px] text-slate-500">
        <span class="inline-block w-1.5 h-1.5 rounded-full" style="background:${{board.color}}"></span>
        ${{escapeHtml(board.name)}}
      </span>`);
    }}
    for (const t of (a.tags || [])) {{
      badges.push(`<span class="inline-flex items-center px-1.5 py-0.5 rounded-full border text-[10px] ${{tagClass(t)}}">${{escapeHtml(t)}}</span>`);
    }}

    const summary = a.summary
      ? `<div class="mt-1 text-[13px] text-slate-600 line-clamp-3">${{escapeHtml(a.summary)}}</div>`
      : "";

    // source line: cluster_size > 1 时显示 "N sources: A · B · C"
    let srcText = "";
    if (a.cluster_size > 1 && (a.sources || []).length > 1) {{
      srcText = `${{a.cluster_size}} sources: ${{a.sources.map(escapeHtml).join(" · ")}}`;
    }} else if (a.source) {{
      srcText = escapeHtml(a.source);
    }} else if ((a.sources || []).length) {{
      srcText = escapeHtml(a.sources[0]);
    }}

    const when = fmtTime(a.ts);
    const footMeta = [srcText, when].filter(Boolean).join(" · ");

    // 整卡片可点 (链接覆盖整张卡), 用 ``<a>`` 包 inner 区, 但 inner 区里的
    // <span> 都不抢点击; 这样点 tag 才不至于跳走。
    const inner = `
      <div class="flex flex-wrap items-center gap-1.5">${{badges.join("")}}</div>
      <h2 class="mt-1.5 text-[15px] font-medium leading-snug text-slate-900 line-clamp-3">${{escapeHtml(a.title)}}</h2>
      ${{summary}}
      <div class="mt-2 text-[11px] text-slate-500">${{footMeta || "&nbsp;"}}</div>
    `;
    if (isLink) {{
      return `<a href="${{url}}" target="_blank" rel="noopener"
        class="block bg-white border border-slate-200 rounded-lg p-3
               hover:border-slate-300 hover:shadow-sm transition active:scale-[0.997]">${{inner}}</a>`;
    }}
    return `<div class="bg-white border border-slate-200 rounded-lg p-3">${{inner}}</div>`;
  }}).join("");
}}

function renderAll() {{
  document.getElementById("meta-generated").textContent = DATA.generated_at_display;
  document.getElementById("footer-generated").textContent = DATA.generated_at_display;
  document.getElementById("meta-total").textContent = DATA.total;
  renderBoardTabs();
  renderTagBar();
  renderArticles();
}}

// ----- Wire up -----
window.addEventListener("hashchange", () => {{
  parseHash();
  const searchEl = document.getElementById("search");
  if (searchEl.value !== state.q) searchEl.value = state.q;
  renderAll();
}});

document.getElementById("search").addEventListener("input", (e) => {{
  state.q = e.target.value || "";
  syncHash(); renderAll();
}});

// init
parseHash();
document.getElementById("search").value = state.q;
renderAll();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    output_dir: Path = Path("docs"),
    per_board_limit: int = 30,
    lookback_hours: int = 168,
    public_url: str | None = None,
) -> dict[str, Any]:
    """生成 ``output_dir/index.html`` 并返回元信息。

    参数:
        output_dir: 输出目录。GitHub Pages 推荐用项目根下的 ``docs/``
            (仓库 Settings → Pages → Source: ``main`` 分支 ``/docs`` 子目录)。
            目录会被自动创建。
        per_board_limit: 每个板块最多导出多少条 (cluster-merged 文章; 默认 30)。
            该上限直接传给 ``list_articles``。
        lookback_hours: 时间窗口, 早于此的文章会被滤掉。默认 168 (7 天),
            和邮件页脚 "数据范围 7 天" 文案对齐。
        public_url: 若提供, 会写到 HTML 的 ``<link rel="canonical">``,
            方便分享时 OpenGraph 工具去重。可以从 ``TRENDRADAR_PUBLIC_URL``
            读取后透传给本函数。

    返回:
        ``{"path": str, "boards": {key: int}, "total": int, "bytes": int}``
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "index.html"

    # public_url 可以从环境变量兜底, 这样 ``--snapshot`` 不传参也能拿到
    # canonical 链接 (跟邮件页脚里的链接保持一致)。
    effective_public_url = (
        public_url
        if public_url is not None
        else (os.getenv("TRENDRADAR_PUBLIC_URL") or "").strip() or None
    )
    if effective_public_url:
        effective_public_url = effective_public_url.rstrip("/")

    boards_payload, counts, total = _collect_all_boards(
        per_board_limit=per_board_limit,
        lookback_hours=lookback_hours,
    )

    now_local = datetime.now().astimezone()
    generated_at_local = now_local.strftime("%Y-%m-%d %H:%M %Z").strip()
    generated_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    html_text = _render_html(
        boards_payload=boards_payload,
        total=total,
        generated_at_local=generated_at_local,
        generated_at_iso=generated_at_iso,
        lookback_hours=lookback_hours,
        public_url=effective_public_url,
    )

    out_path.write_text(html_text, encoding="utf-8")
    size = out_path.stat().st_size

    if size >= _SIZE_WARN_BYTES:
        mb = size / (1024 * 1024)
        print(
            f"[snapshot] WARN: size={mb:.2f} MB (>{_SIZE_WARN_BYTES / 1024 / 1024:.0f} MB). "
            "Consider lowering per_board_limit or trimming summaries.",
            flush=True,
        )

    return {
        "path": str(out_path),
        "boards": counts,
        "total": total,
        "bytes": size,
    }

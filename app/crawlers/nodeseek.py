"""NodeSeek 最新主题（HTML 抓取）.

NodeSeek 没有公开的 JSON API，未登录也能访问首页主题列表。这里抓首页 HTML
然后用 BeautifulSoup 解析。如果某天页面结构改了，调整 _SELECTORS 即可。
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from .base import BaseCrawler, TopicItem


_HOMEPAGE = "https://www.nodeseek.com/"

# 尝试多个可能的选择器，按顺序匹配。NodeSeek 偶尔会改 class 名。
_ITEM_SELECTORS = (
    "li.post-list-item",
    "div.post-list-item",
    "tr.post-list-item",
    "article.topic",
)
_TITLE_SELECTORS = (
    "a.post-title",
    "a.title",
    "h2 a",
    ".main-link a",
)


class NodeSeekCrawler(BaseCrawler):
    key = "nodeseek"
    display_name = "NodeSeek"
    region = "CN"
    url = "https://www.nodeseek.com/"

    def headers(self) -> dict:
        h = super().headers()
        h["Accept"] = "text/html,application/xhtml+xml"
        return h

    async def fetch(self) -> list[TopicItem]:
        resp = await self.get(_HOMEPAGE)
        html = resp.text

        soup = BeautifulSoup(html, "lxml")

        # 找到第一个能匹配出列表项的选择器
        rows = []
        for sel in _ITEM_SELECTORS:
            rows = soup.select(sel)
            if rows:
                break

        # 退化路径：直接抓所有指向 /post/xxx 的链接
        if not rows:
            rows = [a for a in soup.select('a[href^="/post/"]')]

        items: list[TopicItem] = []
        seen_urls: set[str] = set()
        for row in rows:
            link = None
            for sel in _TITLE_SELECTORS:
                link = row.select_one(sel)
                if link:
                    break
            if link is None and row.name == "a":
                link = row
            if link is None:
                continue

            title = link.get_text(strip=True)
            href = link.get("href") or ""
            if not title or not href:
                continue

            full = href if href.startswith("http") else f"https://www.nodeseek.com{href}"
            if full in seen_urls:
                continue
            seen_urls.add(full)

            # 评论/查看数（如有）
            stats = " ".join(
                t.get_text(" ", strip=True)
                for t in row.select(".info, .stats, .post-info, .meta")
            )

            items.append(
                TopicItem(
                    title=title,
                    url=full,
                    score=None,
                    extra={"stats": stats[:120]} if stats else {},
                )
            )
            if len(items) >= 40:
                break
        return items

"""知乎热榜.

知乎的 v3 热榜 API 现在需要 x-zse-96 签名 + 登录态，未登录直接 401，且签名算法
多变易碎。所以默认改用 https://www.zhihu.com/billboard 页面内嵌的 JSON
（script#js-initialData），它不需要登录也不需要签名。

若设置了环境变量 TRENDRADAR_ZHIHU_COOKIE（登录后的完整 cookie），会优先尝试
官方 v3 API，失败再回退到 billboard。
"""
from __future__ import annotations

import json
import os

import httpx
from bs4 import BeautifulSoup

from .base import BaseCrawler, TopicItem

_API = (
    "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total"
    "?limit=50&desktop=true"
)
_BILLBOARD = "https://www.zhihu.com/billboard"


def _score_from_text(text: str) -> float | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    return float(digits) if digits else None


class ZhihuCrawler(BaseCrawler):
    key = "zhihu"
    display_name = "知乎热榜"
    region = "CN"
    url = "https://www.zhihu.com/hot"

    def headers(self) -> dict:
        h = super().headers()
        h.update(
            {
                "Referer": "https://www.zhihu.com/",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
        )
        cookie = os.getenv("TRENDRADAR_ZHIHU_COOKIE")
        if cookie:
            # Strip stray whitespace/newlines; header values may not start with
            # a space (httpx/h11 raises LocalProtocolError otherwise).
            h["Cookie"] = " ".join(cookie.split()).strip()
        return h

    async def fetch(self) -> list[TopicItem]:
        if os.getenv("TRENDRADAR_ZHIHU_COOKIE"):
            try:
                items = await self._fetch_api()
                if items:
                    return items
            except httpx.HTTPError:
                pass
        return await self._fetch_billboard()

    async def _fetch_api(self) -> list[TopicItem]:
        resp = await self.get(_API)
        data = resp.json()
        items: list[TopicItem] = []
        for entry in data.get("data") or []:
            target = entry.get("target") or {}
            title = target.get("title") or target.get("title_area", {}).get("text")
            if not title:
                continue
            tid = target.get("id")
            url = f"https://www.zhihu.com/question/{tid}" if tid else None
            detail = entry.get("detail_text") or ""
            items.append(
                TopicItem(
                    title=title,
                    url=url,
                    score=_score_from_text(detail),
                    extra={"detail": detail} if detail else {},
                )
            )
        return items

    async def _fetch_billboard(self) -> list[TopicItem]:
        # Share one client so the cookie set by the homepage visit (d_c0 etc.)
        # is reused on the billboard request, which helps get past the WAF.
        async with await self.client() as client:
            if "Cookie" not in self.headers():
                try:
                    await client.get("https://www.zhihu.com/")
                except httpx.HTTPError:
                    pass
            resp = await client.get(_BILLBOARD)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "lxml")
        tag = soup.select_one("script#js-initialData")
        raw = tag.text if tag else ""
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        hot = (
            ((data.get("initialState") or {}).get("topstory") or {}).get("hotList")
            or []
        )
        items: list[TopicItem] = []
        for entry in hot:
            target = entry.get("target") or {}
            title = (target.get("titleArea") or {}).get("text")
            if not title:
                continue
            link = (target.get("link") or {}).get("url")
            metrics = (target.get("metricsArea") or {}).get("text") or ""
            items.append(
                TopicItem(
                    title=title,
                    url=link,
                    score=_score_from_text(metrics),
                    extra={"metric": metrics} if metrics else {},
                )
            )
        return items

"""B 站热门视频."""
from __future__ import annotations

from .base import BaseCrawler, TopicItem


class BilibiliCrawler(BaseCrawler):
    key = "bilibili"
    display_name = "B 站热门"
    region = "CN"
    url = "https://www.bilibili.com/v/popular/all"

    async def fetch(self) -> list[TopicItem]:
        endpoint = "https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1"
        resp = await self.get(endpoint)
        data = resp.json()

        videos = (data.get("data") or {}).get("list") or []
        items: list[TopicItem] = []
        for v in videos:
            title = v.get("title")
            if not title:
                continue
            bvid = v.get("bvid")
            video_url = (
                v.get("short_link_v2")
                or (f"https://www.bilibili.com/video/{bvid}" if bvid else None)
            )
            stat = v.get("stat") or {}
            views = stat.get("view")
            owner = (v.get("owner") or {}).get("name") or ""
            items.append(
                TopicItem(
                    title=title,
                    url=video_url,
                    score=float(views) if views else None,
                    extra={"up": owner} if owner else {},
                )
            )
        return items

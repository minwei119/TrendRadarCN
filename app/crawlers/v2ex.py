"""V2EX 热门主题."""
from __future__ import annotations

from .base import BaseCrawler, TopicItem


class V2exCrawler(BaseCrawler):
    key = "v2ex"
    display_name = "V2EX 热门"
    region = "CN"
    url = "https://www.v2ex.com/?tab=hot"

    async def fetch(self) -> list[TopicItem]:
        endpoint = "https://www.v2ex.com/api/topics/hot.json"
        resp = await self.get(endpoint)
        data = resp.json()

        items: list[TopicItem] = []
        for entry in data or []:
            title = entry.get("title")
            if not title:
                continue
            items.append(
                TopicItem(
                    title=title,
                    url=entry.get("url"),
                    score=float(entry.get("replies") or 0),
                    extra={
                        "node": (entry.get("node") or {}).get("title", ""),
                        "author": (entry.get("member") or {}).get("username", ""),
                    },
                )
            )
        return items

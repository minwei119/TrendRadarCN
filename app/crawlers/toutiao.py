"""今日头条热榜."""
from __future__ import annotations

from .base import BaseCrawler, TopicItem


class ToutiaoCrawler(BaseCrawler):
    key = "toutiao"
    display_name = "今日头条"
    region = "CN"
    url = "https://www.toutiao.com/hot-event/hot-board/"

    async def fetch(self) -> list[TopicItem]:
        endpoint = (
            "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
        )
        resp = await self.get(endpoint)
        data = resp.json()

        items: list[TopicItem] = []
        for entry in data.get("data") or []:
            title = entry.get("Title") or entry.get("title")
            if not title:
                continue
            url = entry.get("Url") or entry.get("url")
            hot = entry.get("HotValue") or entry.get("hot_value")
            items.append(
                TopicItem(
                    title=title,
                    url=url,
                    score=float(hot) if hot else None,
                )
            )
        return items

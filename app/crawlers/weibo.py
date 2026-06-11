"""微博热搜."""
from __future__ import annotations

from .base import BaseCrawler, TopicItem


class WeiboCrawler(BaseCrawler):
    key = "weibo"
    display_name = "微博热搜"
    region = "CN"
    url = "https://s.weibo.com/top/summary"

    def headers(self) -> dict:
        h = super().headers()
        h["Referer"] = "https://weibo.com/"
        return h

    async def fetch(self) -> list[TopicItem]:
        resp = await self.get("https://weibo.com/ajax/side/hotSearch")
        data = resp.json()

        realtime = (data.get("data") or {}).get("realtime") or []
        items: list[TopicItem] = []
        for entry in realtime:
            word = entry.get("word")
            if not word:
                continue
            num = entry.get("num") or entry.get("raw_hot")
            label = entry.get("label_name") or entry.get("icon_desc") or ""
            items.append(
                TopicItem(
                    title=word,
                    url=f"https://s.weibo.com/weibo?q=%23{word}%23",
                    score=float(num) if num else None,
                    extra={"label": label} if label else {},
                )
            )
        return items

"""百度热搜 (PC 端 board API)."""
from __future__ import annotations

from .base import BaseCrawler, TopicItem

_ENDPOINT = "https://top.baidu.com/api/board?platform=pc&tab=realtime"


class BaiduCrawler(BaseCrawler):
    key = "baidu"
    display_name = "百度热搜"
    region = "CN"
    url = "https://top.baidu.com/board?tab=realtime"

    def headers(self) -> dict:
        h = super().headers()
        h["Referer"] = "https://top.baidu.com/board?tab=realtime"
        return h

    async def fetch(self) -> list[TopicItem]:
        resp = await self.get(_ENDPOINT)
        data = resp.json()

        cards = ((data.get("data") or {}).get("cards") or [])
        items: list[TopicItem] = []
        seen: set[str] = set()
        for card in cards:
            # realtime tab 同时有置顶(topContent)与普通(content)两块
            for key in ("topContent", "content"):
                for entry in card.get(key) or []:
                    word = entry.get("word") or entry.get("query")
                    if not word or word in seen:
                        continue
                    seen.add(word)
                    score = entry.get("hotScore") or entry.get("heatScore")
                    url = entry.get("url") or entry.get("rawUrl")
                    desc = entry.get("desc") or ""
                    items.append(
                        TopicItem(
                            title=word,
                            url=url,
                            score=float(score) if score else None,
                            extra={"desc": desc} if desc else {},
                        )
                    )
        return items

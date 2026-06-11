"""掘金热门文章 (recommend API).

掘金的全站推荐/热门 feed 接口需要 POST，body 里指定 client_type 和排序方式。
sort_type=3 是"按热度"。这个接口是页面前端用的，无需登录。
"""
from __future__ import annotations

from .base import BaseCrawler, TopicItem


_API = "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"


class JuejinCrawler(BaseCrawler):
    key = "juejin"
    display_name = "掘金热门"
    region = "CN"
    url = "https://juejin.cn/hot/articles"

    def headers(self) -> dict:
        h = super().headers()
        h["Content-Type"] = "application/json"
        h["Referer"] = "https://juejin.cn/"
        h["Origin"] = "https://juejin.cn"
        return h

    async def fetch(self) -> list[TopicItem]:
        payload = {
            "id_type": 2,
            "client_type": 2608,
            "sort_type": 3,
            "cursor": "0",
            "limit": 30,
        }
        resp = await self.post(_API, json=payload)
        data = resp.json()

        items: list[TopicItem] = []
        for entry in (data.get("data") or []):
            info = (entry.get("item_info") or entry)
            article = info.get("article_info") or {}
            title = article.get("title") or info.get("title")
            if not title:
                continue
            article_id = article.get("article_id") or info.get("article_id")
            url = (
                f"https://juejin.cn/post/{article_id}" if article_id else None
            )
            # 综合热度可以参考 view_count / digg_count / hot_index 等字段
            hot = (
                article.get("hot_index")
                or article.get("view_count")
                or article.get("digg_count")
                or 0
            )
            tags = ", ".join(
                t.get("tag_name", "")
                for t in (entry.get("tags") or [])
                if t.get("tag_name")
            )
            items.append(
                TopicItem(
                    title=title,
                    url=url,
                    score=float(hot),
                    extra={
                        "views": article.get("view_count"),
                        "diggs": article.get("digg_count"),
                        "comments": article.get("comment_count"),
                        "tags": tags,
                    },
                )
            )
        return items

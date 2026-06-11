"""Crawler registry for CN sources."""
from __future__ import annotations

from typing import Iterable

from .base import BaseCrawler
from .weibo import WeiboCrawler
from .zhihu import ZhihuCrawler
from .baidu import BaiduCrawler
from .bilibili import BilibiliCrawler
from .toutiao import ToutiaoCrawler
from .v2ex import V2exCrawler
from .nodeseek import NodeSeekCrawler
from .juejin import JuejinCrawler

_REGISTRY: dict[str, BaseCrawler] = {
    c.key: c
    for c in (
        WeiboCrawler(),
        ZhihuCrawler(),
        BaiduCrawler(),
        BilibiliCrawler(),
        ToutiaoCrawler(),
        V2exCrawler(),
        NodeSeekCrawler(),
        JuejinCrawler(),
    )
}


def iter_crawlers() -> Iterable[BaseCrawler]:
    return _REGISTRY.values()


def get_crawler(key: str) -> BaseCrawler | None:
    return _REGISTRY.get(key)

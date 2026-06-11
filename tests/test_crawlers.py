"""Crawler parsing + retry tests using httpx.MockTransport (no real network)."""
from __future__ import annotations

import httpx
import pytest

from app.crawlers.v2ex import V2exCrawler
from app.crawlers.weibo import WeiboCrawler


def patch_client(monkeypatch, crawler_cls, handler):
    """Replace a crawler's client() with one backed by a MockTransport.

    request()'s retry/proxy/status logic still runs; only the wire is faked.
    """

    async def _client(self, proxy=None):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    monkeypatch.setattr(crawler_cls, "client", _client)


async def test_v2ex_parsing(monkeypatch):
    sample = [
        {
            "title": "Hello World",
            "url": "https://www.v2ex.com/t/1",
            "replies": 7,
            "node": {"title": "技术"},
            "member": {"username": "alice"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "hot.json" in str(request.url)
        return httpx.Response(200, json=sample)

    patch_client(monkeypatch, V2exCrawler, handler)
    items = await V2exCrawler().fetch()
    assert len(items) == 1
    assert items[0].title == "Hello World"
    assert items[0].score == 7.0
    assert items[0].extra["node"] == "技术"


async def test_weibo_parsing(monkeypatch):
    sample = {
        "data": {
            "realtime": [
                {"word": "某热搜", "num": 12345, "label_name": "热"},
                {"word": "", "num": 1},  # empty word should be skipped
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample)

    patch_client(monkeypatch, WeiboCrawler, handler)
    items = await WeiboCrawler().fetch()
    assert len(items) == 1
    assert items[0].title == "某热搜"
    assert items[0].score == 12345.0


async def test_retry_on_429_then_success(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_BACKOFF", "0")  # no real sleep
    monkeypatch.setenv("TRENDRADAR_MAX_RETRIES", "2")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=[{"title": "ok", "replies": 1}])

    patch_client(monkeypatch, V2exCrawler, handler)
    items = await V2exCrawler().fetch()
    assert calls["n"] == 2  # retried exactly once
    assert items[0].title == "ok"


async def test_non_retryable_404_raises(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_MAX_RETRIES", "3")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    patch_client(monkeypatch, V2exCrawler, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await V2exCrawler().fetch()

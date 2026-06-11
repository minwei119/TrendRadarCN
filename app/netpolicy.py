"""Politeness helpers: per-host rate limiting and robots.txt enforcement.

Both are opt-in / configurable via env vars (see ``config.py``). They are kept
out of ``BaseCrawler`` itself so the networking policy lives in one place and
can be shared by every crawler.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RateLimiter:
    """Ensures a minimum interval between requests to the same host.

    Concurrent requests to one host queue behind a per-host lock, so they get
    spaced out by ``min_interval`` seconds. Different hosts run in parallel.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    async def acquire(self, url: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        host = host_of(url)
        async with self._lock(host):
            now = time.monotonic()
            last = self._last.get(host)
            if last is not None:
                wait = min_interval - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids fetching a URL for our user-agent."""


class RobotsCache:
    """Fetches and caches robots.txt per host, then answers can_fetch()."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    async def _load(self, host: str, proxy: str | None) -> RobotFileParser | None:
        robots_url = f"{host}/robots.txt"
        try:
            transport = httpx.AsyncHTTPTransport(retries=1, proxy=proxy)
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=True, transport=transport
            ) as client:
                resp = await client.get(robots_url)
        except httpx.HTTPError:
            # If robots.txt is unreachable, fail open (treat as allowed).
            return None

        if resp.status_code >= 400:
            # No robots.txt (404) or server error -> allowed by convention.
            return None

        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    async def allowed(self, url: str, user_agent: str, proxy: str | None) -> bool:
        host = host_of(url)
        if host not in self._cache:
            async with self._lock(host):
                if host not in self._cache:  # double-check after acquiring lock
                    self._cache[host] = await self._load(host, proxy)
        rp = self._cache[host]
        if rp is None:
            return True
        return rp.can_fetch(user_agent, url)


# Process-wide singletons shared by all crawlers.
RATE_LIMITER = RateLimiter()
ROBOTS = RobotsCache()

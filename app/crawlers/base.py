"""Base crawler interface with retry + proxy rotation."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..config import (
    base_backoff,
    max_retries,
    min_interval,
    proxy_candidates,
    respect_robots,
)
from ..netpolicy import RATE_LIMITER, ROBOTS, RobotsDisallowed, host_of
from ..obs import log_request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# HTTP status codes worth retrying (transient server / rate-limit issues).
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class TopicItem:
    title: str
    url: Optional[str] = None
    score: Optional[float] = None
    extra: dict = field(default_factory=dict)


class BaseCrawler:
    """Subclasses override class attributes and implement async fetch().

    Use ``self.request(...)`` (preferred) or ``self.client(...)`` to perform
    HTTP calls; both gain proxy support automatically. ``request`` additionally
    retries transient failures with exponential backoff and rotates through the
    configured proxy candidates.
    """

    key: str = ""
    display_name: str = ""
    region: str = "CN"
    url: str = ""
    timeout: float = 12.0
    # Connection-level retries handled inside httpx's transport (per attempt).
    connect_retries: int = 1

    def headers(self) -> dict:
        return {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }

    # -- low-level client -------------------------------------------------
    def _build_transport(self, proxy: str | None) -> httpx.AsyncHTTPTransport:
        return httpx.AsyncHTTPTransport(retries=self.connect_retries, proxy=proxy)

    async def client(self, proxy: str | None = None) -> httpx.AsyncClient:
        """Build an AsyncClient. If ``proxy`` is None, the first configured
        proxy candidate (which may itself be direct) is used, so legacy callers
        that do ``await self.client()`` still benefit from proxy config."""
        if proxy is None:
            proxy = proxy_candidates()[0]
        return httpx.AsyncClient(
            headers=self.headers(),
            timeout=self.timeout,
            follow_redirects=True,
            transport=self._build_transport(proxy),
        )

    # -- high-level request with retry + rotation -------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        warmup_url: str | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Perform an HTTP request, retrying transient errors and rotating
        proxies across attempts. Returns a successful Response or raises the
        last error after exhausting attempts.

        If ``warmup_url`` is given, an extra GET to that URL is issued inside
        the same client BEFORE the main request — useful for sites that won't
        return real data until a session cookie is set (Xueqiu, Zhihu, ...).
        Warmup failures are tolerated; the main request still proceeds.
        """
        candidates = proxy_candidates()
        attempts = max_retries() + 1
        backoff = base_backoff()
        interval = min_interval()
        ua = self.headers().get("User-Agent", "*")
        last_exc: Exception | None = None

        host = host_of(url)

        # Robots.txt gate (opt-in). Checked once; uses the first proxy candidate.
        if respect_robots():
            allowed = await ROBOTS.allowed(url, ua, candidates[0])
            if not allowed:
                log_request(
                    source=self.key, method=method, url=url, host=host,
                    ok=False, error="RobotsDisallowed", outcome="robots_blocked",
                )
                raise RobotsDisallowed(f"robots.txt disallows {url} for UA '{ua}'")

        for attempt in range(attempts):
            proxy = candidates[attempt % len(candidates)]
            started = time.perf_counter()
            try:
                # Politeness: space out requests to the same host.
                await RATE_LIMITER.acquire(url, interval)
                async with await self.client(proxy=proxy) as client:
                    if warmup_url:
                        try:
                            await client.get(warmup_url)
                        except Exception:
                            # Best-effort: a failing warmup shouldn't block
                            # the main request. We still try it once.
                            pass
                    resp = await client.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                log_request(
                    source=self.key, method=method, url=url, host=host,
                    attempt=attempt + 1, attempts=attempts,
                    proxy=proxy or "direct", status=resp.status_code,
                    ok=True, retried=attempt > 0,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                return resp
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                log_request(
                    source=self.key, method=method, url=url, host=host,
                    attempt=attempt + 1, attempts=attempts,
                    proxy=proxy or "direct", status=status,
                    ok=False, retried=attempt > 0,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
                # 4xx (other than the retryable set) won't fix themselves: stop.
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if code not in RETRY_STATUS:
                        raise
                if attempt < attempts - 1:
                    delay = backoff * (2 ** attempt) + random.uniform(0, backoff)
                    await asyncio.sleep(min(delay, 8.0))

        assert last_exc is not None
        raise last_exc

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def fetch(self) -> list[TopicItem]:  # pragma: no cover
        raise NotImplementedError

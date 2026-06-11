"""Runtime configuration for crawling (proxies, retries, backoff).

Everything is driven by environment variables so you can tune behaviour
without touching code:

    TRENDRADAR_PROXIES     Comma-separated proxy URLs to rotate through, e.g.
                           "http://127.0.0.1:7890,http://127.0.0.1:1080".
    TRENDRADAR_PROXY       Single proxy (alias; merged into the list above).
    TRENDRADAR_USE_DIRECT  "1"/"true" to also try a direct (no-proxy) attempt
                           in the rotation. Default: true.
    TRENDRADAR_MAX_RETRIES Extra attempts after the first try. Default: 2
                           (so up to 3 attempts total).
    TRENDRADAR_BACKOFF     Base backoff seconds for exponential retry.
                           Default: 0.8.
    TRENDRADAR_MIN_INTERVAL Minimum seconds between requests to the same host
                           (politeness throttle). Default: 0 (disabled).
    TRENDRADAR_RESPECT_ROBOTS "1"/"true" to honour each site's robots.txt.
                           Default: false (many JSON/API endpoints are
                           disallowed by robots.txt even though they are the
                           site's own front-end APIs).

If neither TRENDRADAR_PROXIES nor TRENDRADAR_PROXY is set, the standard
HTTPS_PROXY / HTTP_PROXY environment variables are honoured automatically.
"""
from __future__ import annotations

import os


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def configured_proxies() -> list[str]:
    """Explicit proxies from TRENDRADAR_PROXIES / TRENDRADAR_PROXY."""
    raw: list[str] = []
    multi = os.getenv("TRENDRADAR_PROXIES", "")
    if multi:
        raw.extend(p.strip() for p in multi.split(",") if p.strip())
    single = os.getenv("TRENDRADAR_PROXY", "")
    if single.strip():
        raw.append(single.strip())
    # de-duplicate, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in raw:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def env_proxy() -> str | None:
    """Fallback single proxy from the conventional env vars."""
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or None
    )


def use_direct() -> bool:
    return _truthy(os.getenv("TRENDRADAR_USE_DIRECT"), default=True)


def proxy_candidates() -> list[str | None]:
    """Resolve the ordered list of proxies to rotate through.

    Each element is a proxy URL, or ``None`` meaning "go direct". The retry
    loop cycles through this list, so a list with both a proxy and ``None``
    will alternate proxy/direct across attempts.
    """
    explicit = configured_proxies()
    if explicit:
        candidates: list[str | None] = list(explicit)
        if use_direct():
            candidates.append(None)
        return candidates

    fallback = env_proxy()
    if fallback:
        # Honour HTTPS_PROXY but still allow a direct retry as a safety net.
        return [fallback, None] if use_direct() else [fallback]

    return [None]


def max_retries() -> int:
    try:
        return max(0, int(os.getenv("TRENDRADAR_MAX_RETRIES", "2")))
    except ValueError:
        return 2


def base_backoff() -> float:
    try:
        return max(0.0, float(os.getenv("TRENDRADAR_BACKOFF", "0.8")))
    except ValueError:
        return 0.8


def min_interval() -> float:
    """Minimum seconds between requests to the same host (0 = disabled)."""
    try:
        return max(0.0, float(os.getenv("TRENDRADAR_MIN_INTERVAL", "0")))
    except ValueError:
        return 0.0


def respect_robots() -> bool:
    return _truthy(os.getenv("TRENDRADAR_RESPECT_ROBOTS"), default=False)

"""Tests for proxy/retry configuration resolution."""
from __future__ import annotations

from app import config

_VARS = [
    "TRENDRADAR_PROXIES",
    "TRENDRADAR_PROXY",
    "TRENDRADAR_USE_DIRECT",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
]


def _clear(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_default_is_direct_only(monkeypatch):
    _clear(monkeypatch)
    assert config.proxy_candidates() == [None]


def test_explicit_proxies_plus_direct(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRENDRADAR_PROXIES", "http://a:1,http://b:2")
    assert config.proxy_candidates() == ["http://a:1", "http://b:2", None]


def test_explicit_proxies_no_direct(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRENDRADAR_PROXIES", "http://a:1")
    monkeypatch.setenv("TRENDRADAR_USE_DIRECT", "false")
    assert config.proxy_candidates() == ["http://a:1"]


def test_env_https_proxy_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://env:9")
    assert config.proxy_candidates() == ["http://env:9", None]


def test_proxies_deduplicated(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRENDRADAR_PROXIES", "http://a:1,http://a:1")
    monkeypatch.setenv("TRENDRADAR_USE_DIRECT", "false")
    assert config.proxy_candidates() == ["http://a:1"]


def test_max_retries_and_backoff(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_MAX_RETRIES", "5")
    monkeypatch.setenv("TRENDRADAR_BACKOFF", "1.5")
    assert config.max_retries() == 5
    assert config.base_backoff() == 1.5


def test_bad_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("TRENDRADAR_MAX_RETRIES", "abc")
    monkeypatch.setenv("TRENDRADAR_BACKOFF", "xyz")
    assert config.max_retries() == 2
    assert config.base_backoff() == 0.8

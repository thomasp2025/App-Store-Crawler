"""HTTP client behaviour. Network is mocked with respx -- nothing here leaves the box."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from screener.http.cache import DiskCache
from screener.http.client import HardStop, ITunesClient, NotFound, RequestKind
from screener.http.ratelimit import TokenBucket

URL = "https://itunes.apple.com/lookup?id=1&country=us"


@pytest.fixture
def client(config, tmp_path):
    cache = DiskCache(tmp_path / "cache", enabled=True)
    return ITunesClient(config, cache=cache)


class TestTokenBucket:
    async def test_allows_burst_then_throttles(self):
        bucket = TokenBucket(rate_per_minute=60, capacity=2)
        assert await bucket.acquire() == 0.0
        assert await bucket.acquire() == 0.0
        # Third token must wait ~1s at 60/min.
        start = time.monotonic()
        waited = await bucket.acquire()
        assert waited > 0
        assert time.monotonic() - start >= 0.5

    async def test_rejects_nonsense_rate(self):
        with pytest.raises(ValueError):
            TokenBucket(rate_per_minute=0)


class TestDiskCache:
    def test_roundtrip(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=True)
        cache.set(URL, "lookup", '{"a": 1}')
        hit = cache.get(URL, "lookup", ttl_hours=24)
        assert hit is not None and hit.body == '{"a": 1}'

    def test_expired_entry_is_a_miss(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=True)
        cache.set(URL, "lookup", "x")
        assert cache.get(URL, "lookup", ttl_hours=0) is None

    def test_disabled_cache_never_stores(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=False)
        cache.set(URL, "lookup", "x")
        assert cache.get(URL, "lookup", 24) is None

    def test_corrupt_entry_is_a_miss_not_a_crash(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=True)
        cache.set(URL, "lookup", "x")
        path = cache._path(URL, "lookup")
        path.write_text("{ not json")
        assert cache.get(URL, "lookup", 24) is None

    def test_clear_by_kind(self, tmp_path):
        cache = DiskCache(tmp_path, enabled=True)
        cache.set(URL, "lookup", "x")
        cache.set(URL + "&b=1", "search", "y")
        assert cache.clear("lookup") == 1
        assert cache.get(URL + "&b=1", "search", 24) is not None


class TestClientBehaviour:
    @respx.mock
    async def test_cache_hit_costs_no_network_call(self, client):
        route = respx.get(URL).mock(return_value=httpx.Response(200, text='{"ok":1}'))
        first = await client.get(URL, RequestKind.LOOKUP)
        second = await client.get(URL, RequestKind.LOOKUP)
        assert route.call_count == 1
        assert first.from_cache is False and second.from_cache is True
        assert client.stats["cache_hits"] == 1
        await client.aclose()

    @respx.mock
    async def test_no_cache_flag_bypasses(self, client):
        route = respx.get(URL).mock(return_value=httpx.Response(200, text="{}"))
        await client.get(URL, RequestKind.LOOKUP, use_cache=False)
        await client.get(URL, RequestKind.LOOKUP, use_cache=False)
        assert route.call_count == 2
        await client.aclose()

    @respx.mock
    async def test_retries_then_succeeds_on_429(self, client):
        respx.get(URL).mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, text='{"ok":1}'),
            ]
        )
        client.config.http.__dict__["backoff_base_seconds"] = 0.01
        resp = await client.get(URL, RequestKind.LOOKUP)
        assert resp.status_code == 200
        assert client.stats["retries"] == 1
        await client.aclose()

    @respx.mock
    async def test_sustained_403_is_a_hard_stop_not_a_retry_loop(self, client):
        respx.get(URL).mock(return_value=httpx.Response(403))
        client.config.http.__dict__["backoff_base_seconds"] = 0.01
        with pytest.raises(HardStop):
            await client.get(URL, RequestKind.LOOKUP)
        # Stopped at the configured limit rather than burning every retry.
        assert respx.calls.call_count == client.config.http.hard_stop_after_consecutive_403
        await client.aclose()

    @respx.mock
    async def test_terminal_status_is_returned_not_raised(self, client):
        # The reviews feed answers 400 past page 10; that's an answer, not a failure.
        respx.get(URL).mock(return_value=httpx.Response(400, text="err"))
        resp = await client.get(URL, RequestKind.REVIEWS, terminal_statuses=frozenset({400}))
        assert resp.status_code == 400
        await client.aclose()

    @respx.mock
    async def test_unexpected_4xx_raises(self, client):
        respx.get(URL).mock(return_value=httpx.Response(404))
        with pytest.raises(NotFound):
            await client.get(URL, RequestKind.LOOKUP)
        await client.aclose()

    @respx.mock
    async def test_transport_error_is_retried(self, client):
        respx.get(URL).mock(
            side_effect=[
                httpx.ConnectError("boom"),
                httpx.Response(200, text="{}"),
            ]
        )
        client.config.http.__dict__["backoff_base_seconds"] = 0.01
        resp = await client.get(URL, RequestKind.LOOKUP)
        assert resp.status_code == 200
        await client.aclose()

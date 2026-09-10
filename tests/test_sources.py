"""Endpoint wrappers, driven by the recorded fixtures."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from screener.http.cache import DiskCache
from screener.http.client import ITunesClient
from screener.sources.itunes import ITunesSource


@pytest.fixture
def source(config, tmp_path):
    client = ITunesClient(config, cache=DiskCache(tmp_path / "c", enabled=False))
    return ITunesSource(client, config)


class TestSearch:
    @respx.mock
    async def test_parses_fixture(self, source, search_payload):
        respx.get(url__startswith="https://itunes.apple.com/search").mock(
            return_value=httpx.Response(200, text=json.dumps(search_payload))
        )
        apps = await source.search("knitting row counter", use_cache=False)
        assert len(apps) == len(search_payload["results"])
        assert apps[0].track_id
        await source.client.aclose()

    @respx.mock
    async def test_malformed_json_returns_empty_not_crash(self, source):
        respx.get(url__startswith="https://itunes.apple.com/search").mock(
            return_value=httpx.Response(200, text="<html>nope</html>")
        )
        assert await source.search("x", use_cache=False) == []
        await source.client.aclose()


class TestLookup:
    @respx.mock
    async def test_parses_fixture(self, source, lookup_payload):
        respx.get(url__startswith="https://itunes.apple.com/lookup").mock(
            return_value=httpx.Response(200, text=json.dumps(lookup_payload))
        )
        apps = await source.lookup([320606217, 284882215], use_cache=False)
        assert len(apps) == 2
        await source.client.aclose()

    @respx.mock
    async def test_batches_respect_configured_size(self, source, lookup_payload):
        source.config.discovery.__dict__["lookup_batch_size"] = 2
        route = respx.get(url__startswith="https://itunes.apple.com/lookup").mock(
            return_value=httpx.Response(200, text=json.dumps({"resultCount": 0, "results": []}))
        )
        await source.lookup([1, 2, 3, 4, 5], use_cache=False)
        assert route.call_count == 3  # 2 + 2 + 1
        await source.client.aclose()


class TestReviews:
    @respx.mock
    async def test_stops_at_400_end_of_feed(self, source, reviews_payload):
        respx.get(url__regex=r".*page=1.*").mock(
            return_value=httpx.Response(200, text=json.dumps(reviews_payload))
        )
        respx.get(url__regex=r".*page=[2-9].*").mock(return_value=httpx.Response(400))
        respx.get(url__regex=r".*page=10.*").mock(return_value=httpx.Response(400))
        reviews = await source.reviews(320606217, use_cache=False)
        assert len(reviews) == len(reviews_payload["feed"]["entry"])
        await source.client.aclose()

    @respx.mock
    async def test_dedups_repeated_pages(self, source, reviews_payload):
        # Same page served twice: the second contributes nothing and ends the crawl.
        respx.get(url__regex=r".*customerreviews.*").mock(
            return_value=httpx.Response(200, text=json.dumps(reviews_payload))
        )
        reviews = await source.reviews(320606217, use_cache=False, max_pages=5)
        ids = [r.review_id for r in reviews]
        assert len(ids) == len(set(ids))
        await source.client.aclose()

    @respx.mock
    async def test_all_timestamps_are_utc_aware(self, source, reviews_payload):
        respx.get(url__regex=r".*customerreviews.*").mock(
            return_value=httpx.Response(200, text=json.dumps(reviews_payload))
        )
        reviews = await source.reviews(320606217, use_cache=False, max_pages=1)
        assert all(r.updated_at.tzinfo is not None for r in reviews)
        await source.client.aclose()

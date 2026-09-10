"""Rate-limited, cached, retrying HTTP client for Apple's public endpoints.

Everything else in the pipeline goes through this. Behaviour that is deliberate:

- One global token bucket shared across all requests.
- Disk cache checked before the bucket, so cache hits cost no rate budget.
- Exponential backoff with jitter on 429/5xx.
- Sustained 403 is a HARD STOP, not a retry loop. Apple does not publish limits and
  hammering a block is how an IP earns a longer one.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

import httpx

from screener.config import Config
from screener.http.cache import DiskCache
from screener.http.ratelimit import TokenBucket
from screener.logging import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RequestKind(StrEnum):
    """Logical response kind. Drives cache TTL and cache sharding."""

    SEARCH = "search"
    LOOKUP = "lookup"
    REVIEWS = "reviews"


class HardStop(RuntimeError):
    """Apple is actively refusing us. Stop the run; do not retry."""


class NotFound(RuntimeError):
    """Endpoint returned a terminal 4xx that means 'no such resource', not 'blocked'."""


@dataclass
class Response:
    body: str
    url: str
    from_cache: bool
    status_code: int

    def json(self) -> Any:
        import json

        return json.loads(self.body)


class ITunesClient:
    def __init__(
        self,
        config: Config,
        *,
        cache: DiskCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        http_cfg = config.http
        self.cache = (
            cache
            if cache is not None
            else DiskCache(config.cache.dir, enabled=config.cache.enabled)
        )
        self._bucket = TokenBucket(http_cfg.rate_limit_per_minute)
        self._sem = asyncio.Semaphore(http_cfg.max_concurrency)
        self._consecutive_403 = 0
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": http_cfg.user_agent,
                "Accept": "application/json, text/javascript;q=0.9, */*;q=0.5",
            },
            timeout=http_cfg.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self.stats = {"network": 0, "cache_hits": 0, "retries": 0}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backoff_delay(self, attempt: int) -> float:
        cfg = self.config.http
        raw: float = cfg.backoff_base_seconds * (2**attempt)
        capped: float = min(raw, cfg.backoff_max_seconds)
        jitter: float = capped * cfg.backoff_jitter
        offset: float = random.uniform(-jitter, jitter)
        delay: float = max(0.0, capped + offset)
        return delay

    async def get(
        self,
        url: str,
        kind: RequestKind,
        *,
        use_cache: bool = True,
        terminal_statuses: frozenset[int] = frozenset(),
    ) -> Response:
        """Fetch `url`, honouring cache, rate limit and retry policy.

        `terminal_statuses` are codes the caller treats as a meaningful answer rather
        than an error -- e.g. the review feed returns 400 past page 10.
        """
        ttl = self.config.cache.ttl_hours_for(kind.value)
        if use_cache:
            hit = self.cache.get(url, kind.value, ttl)
            if hit is not None:
                self.stats["cache_hits"] += 1
                log.info("fetch", url=url, kind=kind.value, cache="hit", status=200)
                return Response(body=hit.body, url=url, from_cache=True, status_code=200)

        last_exc: Exception | None = None
        for attempt in range(self.config.http.max_retries):
            async with self._sem:
                waited = await self._bucket.acquire()
                try:
                    resp = await self._client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    self.stats["retries"] += 1
                    delay = self._backoff_delay(attempt)
                    log.warning(
                        "fetch.transport_error",
                        url=url,
                        kind=kind.value,
                        error=str(exc),
                        attempt=attempt + 1,
                        retry_in=round(delay, 2),
                    )
                    await asyncio.sleep(delay)
                    continue

            self.stats["network"] += 1
            status = resp.status_code

            if status == 200:
                self._consecutive_403 = 0
                if use_cache:
                    self.cache.set(url, kind.value, resp.text)
                log.info(
                    "fetch",
                    url=url,
                    kind=kind.value,
                    cache="miss",
                    status=status,
                    bytes=len(resp.text),
                    rl_wait=round(waited, 2),
                )
                return Response(body=resp.text, url=url, from_cache=False, status_code=status)

            if status in terminal_statuses:
                self._consecutive_403 = 0
                log.info(
                    "fetch", url=url, kind=kind.value, cache="miss", status=status, terminal=True
                )
                return Response(body=resp.text, url=url, from_cache=False, status_code=status)

            if status == 403:
                self._consecutive_403 += 1
                limit = self.config.http.hard_stop_after_consecutive_403
                log.warning(
                    "fetch.forbidden",
                    url=url,
                    kind=kind.value,
                    consecutive=self._consecutive_403,
                    limit=limit,
                )
                if self._consecutive_403 >= limit:
                    raise HardStop(
                        f"{self._consecutive_403} consecutive 403s from Apple "
                        f"(limit {limit}). Stopping the run. Lower "
                        f"http.rate_limit_per_minute and retry later."
                    )
                self.stats["retries"] += 1
                await asyncio.sleep(self._backoff_delay(attempt))
                continue

            if status in RETRYABLE_STATUS:
                self.stats["retries"] += 1
                delay = self._backoff_delay(attempt)
                log.warning(
                    "fetch.retryable",
                    url=url,
                    kind=kind.value,
                    status=status,
                    attempt=attempt + 1,
                    retry_in=round(delay, 2),
                )
                await asyncio.sleep(delay)
                continue

            # Any other 4xx is a client-side mistake; retrying will not fix it.
            log.error("fetch.terminal", url=url, kind=kind.value, status=status)
            raise NotFound(f"{status} for {url}")

        raise NotFound(f"exhausted {self.config.http.max_retries} attempts for {url}") from last_exc

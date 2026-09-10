"""Typed wrappers over the three verified iTunes endpoints.

URL formats confirmed live -- see docs/endpoints.md.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from urllib.parse import quote

from screener.config import Config
from screener.http.client import ITunesClient, NotFound, RequestKind
from screener.logging import get_logger
from screener.sources.models import AppRecord, ReviewRecord, SearchResponse, entries_from_feed

log = get_logger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
REVIEWS_URL = (
    "https://itunes.apple.com/{cc}/rss/customerreviews/"
    "page={page}/id={track_id}/sortby=mostrecent/json"
)

# The reviews feed answers 400 for pages past the ~10-page ceiling. That's an answer
# ("no more pages"), not a failure, so the client returns it instead of raising.
REVIEWS_TERMINAL = frozenset({400, 403, 404})


def _chunk(items: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class ITunesSource:
    def __init__(self, client: ITunesClient, config: Config) -> None:
        self.client = client
        self.config = config

    async def search(
        self,
        term: str,
        *,
        country: str | None = None,
        limit: int | None = None,
        use_cache: bool = True,
    ) -> list[AppRecord]:
        """Search for apps by keyword. Results are in rank order."""
        cc = country or self.config.discovery.country
        lim = limit or self.config.discovery.search_limit
        url = f"{SEARCH_URL}?term={quote(term)}&country={cc}" f"&entity=software&limit={lim}"
        resp = await self.client.get(url, RequestKind.SEARCH, use_cache=use_cache)
        try:
            parsed = SearchResponse.model_validate(resp.json())
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("search.parse_failed", term=term, error=str(exc))
            return []
        log.info("search.ok", term=term, results=len(parsed.results), cached=resp.from_cache)
        return parsed.results

    async def lookup(
        self,
        track_ids: Sequence[int],
        *,
        country: str | None = None,
        use_cache: bool = True,
    ) -> list[AppRecord]:
        """Batch-enrich apps by track ID.

        Apple silently omits IDs it no longer serves, so the caller must diff the
        returned IDs against what it asked for to detect delistings.
        """
        cc = country or self.config.discovery.country
        batch_size = self.config.discovery.lookup_batch_size
        out: list[AppRecord] = []
        for batch in _chunk(list(track_ids), batch_size):
            ids = ",".join(str(t) for t in batch)
            url = f"{LOOKUP_URL}?id={ids}&country={cc}"
            try:
                resp = await self.client.get(url, RequestKind.LOOKUP, use_cache=use_cache)
            except NotFound as exc:
                log.warning("lookup.failed", count=len(batch), error=str(exc))
                continue
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                log.error("lookup.parse_failed", count=len(batch), error=str(exc))
                continue
            for raw in payload.get("results", []):
                # Lookup on a software ID can return non-app wrapperTypes; skip them.
                if raw.get("wrapperType") not in (None, "software"):
                    continue
                try:
                    out.append(AppRecord.model_validate(raw))
                except ValueError as exc:
                    log.warning(
                        "lookup.invalid_record", track_id=raw.get("trackId"), error=str(exc)
                    )
            missing = set(batch) - {a.track_id for a in out}
            if missing:
                log.info("lookup.missing_ids", count=len(missing), sample=sorted(missing)[:5])
        return out

    async def reviews(
        self,
        track_id: int,
        *,
        country: str | None = None,
        max_pages: int | None = None,
        use_cache: bool = True,
    ) -> list[ReviewRecord]:
        """Fetch the most recent reviews, newest first, deduped across pages.

        Pages shift as new reviews land, so the same review can appear on two pages of
        one crawl. Dedup on review ID here as well as at the DB layer.
        """
        cc = country or self.config.reviews.country
        pages = max_pages or self.config.reviews.max_pages
        seen: set[str] = set()
        collected: list[ReviewRecord] = []

        for page in range(1, pages + 1):
            url = REVIEWS_URL.format(cc=cc, page=page, track_id=track_id)
            try:
                resp = await self.client.get(
                    url,
                    RequestKind.REVIEWS,
                    use_cache=use_cache,
                    terminal_statuses=REVIEWS_TERMINAL,
                )
            except NotFound as exc:
                log.warning("reviews.failed", track_id=track_id, page=page, error=str(exc))
                break

            if resp.status_code != 200:
                log.info(
                    "reviews.end_of_feed", track_id=track_id, page=page, status=resp.status_code
                )
                break

            try:
                entries = entries_from_feed(resp.json())
            except json.JSONDecodeError:
                log.warning("reviews.parse_failed", track_id=track_id, page=page)
                break

            if not entries:
                break

            new_on_page = 0
            for entry in entries:
                review = ReviewRecord.from_feed_entry(entry, track_id)
                if review is None or review.review_id in seen:
                    continue
                seen.add(review.review_id)
                collected.append(review)
                new_on_page += 1

            # A page that contributes nothing new means we've caught up to ourselves.
            if new_on_page == 0:
                break

        log.info("reviews.ok", track_id=track_id, count=len(collected))
        return collected

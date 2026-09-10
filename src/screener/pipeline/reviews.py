"""Stage: review ingestion.

Pulls the ~500 most recent reviews per app. Dedup is handled twice -- once across the
pages of a single crawl (pages shift as reviews land) and once at the DB layer, where an
edited review updates its row rather than inserting a second one.
"""

from __future__ import annotations

from dataclasses import dataclass

from screener.config import Config
from screener.db import Database
from screener.logging import get_logger
from screener.sources.itunes import ITunesSource

log = get_logger(__name__)


@dataclass
class ReviewIngestResult:
    apps: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0


async def run_review_ingest(
    db: Database,
    source: ITunesSource,
    config: Config,
    *,
    track_ids: list[int] | None = None,
    use_cache: bool = True,
    max_pages: int | None = None,
) -> ReviewIngestResult:
    ids = track_ids if track_ids is not None else db.all_track_ids()
    result = ReviewIngestResult()

    for track_id in ids:
        if db.get_app(track_id) is None:
            log.warning("reviews.unknown_app", track_id=track_id)
            continue
        reviews = await source.reviews(track_id, use_cache=use_cache, max_pages=max_pages)
        if not reviews:
            result.apps += 1
            continue
        inserted, updated = db.upsert_reviews(reviews)
        result.apps += 1
        result.fetched += len(reviews)
        result.inserted += inserted
        result.updated += updated
        log.info(
            "reviews.stored",
            track_id=track_id,
            fetched=len(reviews),
            inserted=inserted,
            updated=updated,
        )

    return result

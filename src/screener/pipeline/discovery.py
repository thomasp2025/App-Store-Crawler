"""Stage 1 -- Discovery.

Seeds from a hand-written keyword file, searches each term, and records every hit with
its rank. Two deliberate choices:

  * Long-tail terms, not category names. "category name" searches return the funded
    incumbents; the opportunity lives in narrow phrasing.
  * Ranks 50-250 are kept, not just the top 10. The top of a search result is exactly
    where a well-resourced competitor already is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from screener.config import Config
from screener.db import Database
from screener.logging import get_logger
from screener.sources.itunes import ITunesSource

log = get_logger(__name__)


@dataclass
class DiscoveryResult:
    terms_searched: int = 0
    apps_seen: int = 0
    new_apps: int = 0
    per_term: dict[str, int] = field(default_factory=dict)


def load_seeds(path: Path) -> list[str]:
    """Read seeds.txt: one term per line, `#` comments and blanks ignored."""
    if not path.exists():
        return []
    terms = []
    for line in path.read_text("utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return terms


async def run_discovery(
    db: Database,
    source: ITunesSource,
    config: Config,
    *,
    terms: list[str] | None = None,
    use_cache: bool = True,
) -> DiscoveryResult:
    if terms is None:
        terms = load_seeds(config.discovery.seeds_file)
        if not terms:
            log.warning("discovery.no_seeds", path=str(config.discovery.seeds_file))
            return DiscoveryResult()

    result = DiscoveryResult()
    known = set(db.all_track_ids())
    min_rank, max_rank = config.discovery.min_rank, config.discovery.max_rank

    for term in terms:
        keyword_id = db.upsert_keyword(term, source="seed")
        apps = await source.search(term, use_cache=use_cache)

        ranked: list[tuple[int, int]] = []
        for rank, app in enumerate(apps, start=1):
            if rank < min_rank or rank > max_rank:
                continue
            db.upsert_app(app)
            # Search results carry the full field set, so seed a snapshot immediately
            # rather than waiting for the next lookup pass. Time series starts on day one.
            db.upsert_snapshot(app)
            ranked.append((app.track_id, rank))
            if app.track_id not in known:
                known.add(app.track_id)
                result.new_apps += 1

        db.record_hits(keyword_id, ranked)
        db.mark_keyword_searched(keyword_id, len(ranked))
        result.terms_searched += 1
        result.apps_seen += len(ranked)
        result.per_term[term] = len(ranked)
        log.info("discovery.term_done", term=term, kept=len(ranked), returned=len(apps))

    return result

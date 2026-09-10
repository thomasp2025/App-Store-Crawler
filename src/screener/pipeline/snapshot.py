"""Stage: snapshot runner.

The single most valuable thing this tool builds. Nobody can retroactively query "was this
app's review velocity growing 6 months ago" -- so this runs daily from day one, even
against a small seed set. Scoring can be backfilled; missed days cannot.

Idempotent by construction: one row per (track_id, UTC date), enforced by the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from screener.config import Config
from screener.db import Database, utc_now
from screener.logging import get_logger
from screener.sources.itunes import ITunesSource

log = get_logger(__name__)


@dataclass
class SnapshotResult:
    requested: int = 0
    captured: int = 0
    new_rows: int = 0
    updated_rows: int = 0
    missing: list[int] = field(default_factory=list)


async def run_snapshot(
    db: Database,
    source: ITunesSource,
    config: Config,
    *,
    track_ids: list[int] | None = None,
    use_cache: bool = True,
    captured_at: datetime | None = None,
) -> SnapshotResult:
    """Re-look-up every tracked app and record today's row."""
    ids = track_ids if track_ids is not None else db.all_track_ids()
    result = SnapshotResult(requested=len(ids))
    if not ids:
        log.warning("snapshot.no_apps")
        return result

    ts = captured_at or utc_now()
    records = await source.lookup(ids, use_cache=use_cache)
    returned = {r.track_id for r in records}

    for app in records:
        db.upsert_app(app)
        if db.upsert_snapshot(app, captured_at=ts):
            result.new_rows += 1
        else:
            result.updated_rows += 1
        result.captured += 1

    # Apple omits IDs it no longer serves. A delisted incumbent is itself a signal, so
    # surface it rather than letting it vanish silently.
    result.missing = sorted(set(ids) - returned)
    if result.missing:
        log.warning(
            "snapshot.apps_missing_from_lookup",
            count=len(result.missing),
            sample=result.missing[:10],
        )

    log.info(
        "snapshot.done",
        requested=result.requested,
        captured=result.captured,
        new=result.new_rows,
        updated=result.updated_rows,
        missing=len(result.missing),
    )
    return result

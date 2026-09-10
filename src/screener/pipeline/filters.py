"""Stage 2 -- Hard filters.

Cheap auto-reject, runs on every snapshot. Every threshold comes from config.toml; none
are hardcoded, because these will be tuned constantly.

A filter whose input is missing does NOT reject. Early in the project most apps have no
velocity_trend and thin review history, and rejecting on absent data would empty the
funnel for reasons that have nothing to do with the apps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from screener.config import Config
from screener.db import Database, parse_iso, utc_now
from screener.pipeline.context import AppMetrics


@dataclass
class FilterVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # why it was rejected
    skipped: list[str] = field(default_factory=list)  # filters that lacked data

    @property
    def kill_reason(self) -> str | None:
        return "; ".join(self.reasons) if self.reasons else None


def apply_hard_filters(
    m: AppMetrics, config: Config, *, db: Database | None = None
) -> FilterVerdict:
    f = config.filters
    reasons: list[str] = []
    skipped: list[str] = []

    if m.staleness_days is None:
        skipped.append("staleness")
    elif m.staleness_days < f.min_staleness_days:
        reasons.append(
            f"staleness {m.staleness_days}d < {f.min_staleness_days}d (still maintained)"
        )

    if m.velocity_trend is None:
        skipped.append("velocity_trend")
    elif m.velocity_trend < f.min_velocity_trend:
        reasons.append(
            f"velocity_trend {m.velocity_trend:.2f} < {f.min_velocity_trend} (market dying)"
        )

    if m.average_user_rating is None:
        skipped.append("rating")
    else:
        if m.average_user_rating > f.max_average_user_rating:
            reasons.append(
                f"rating {m.average_user_rating:.2f} > {f.max_average_user_rating} "
                f"(users satisfied)"
            )
        if m.average_user_rating < f.min_average_user_rating:
            reasons.append(
                f"rating {m.average_user_rating:.2f} < {f.min_average_user_rating} "
                f"(broken market, not a beatable app)"
            )

    if m.user_rating_count is None:
        skipped.append("rating_count")
    elif m.user_rating_count < f.min_user_rating_count:
        reasons.append(
            f"rating_count {m.user_rating_count} < {f.min_user_rating_count} "
            f"(too small to prove demand)"
        )

    if db is not None:
        fresh = count_fresh_competitors(db, m.track_id, config)
        if fresh is None:
            skipped.append("competitors")
        elif fresh >= f.max_fresh_competitors:
            reasons.append(
                f"{fresh} competitors in keyword cluster shipped within "
                f"{f.competitor_fresh_days}d (market actively served)"
            )

    return FilterVerdict(passed=not reasons, reasons=reasons, skipped=skipped)


def count_fresh_competitors(db: Database, track_id: int, config: Config) -> int | None:
    """Apps found by the same keywords that shipped recently.

    Co-discovery by a keyword is a rough proxy for "same market" -- good enough to catch
    the case where two funded teams are already actively serving these users.
    """
    cohort = db.keyword_cohort(
        track_id,
        top_rank=config.filters.competitor_cohort_top_rank,
        same_genre=config.filters.competitor_require_same_genre,
    )
    if not cohort:
        return None
    now = utc_now()
    cutoff_days = config.filters.competitor_fresh_days
    fresh = 0
    for other in cohort:
        snap = db.latest_snapshot(other)
        if snap is None:
            continue
        released = parse_iso(snap["current_version_release_date"])
        if released is None:
            continue
        if (now - released).days <= cutoff_days:
            fresh += 1
    return fresh

"""Derived metrics. Pure functions over rows -- no DB access, no network, no clock reads
except through an injectable `now`, so every function is deterministic under test.

The two signals that drive the whole thesis are computed here:
  demand velocity  <- review timestamps
  incumbent decay  <- staleness + rating_decay
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# A review stream is any sequence of objects exposing `updated_at` (UTC-aware datetime)
# and `rating`. sqlite3.Row, ReviewRecord and plain dicts all work via _get.


def _get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, None)


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _timestamps(reviews: Sequence[Any]) -> list[datetime]:
    out = [_as_dt(_get(r, "updated_at")) for r in reviews]
    return sorted(t for t in out if t is not None)


@dataclass(frozen=True)
class Velocity:
    """Reviews per day, plus the honesty flags that make the number interpretable."""

    value: float | None
    method: str  # 'window' | 'span' | 'none'
    window_days: int
    sample_size: int
    saturated: bool = False
    span_days: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_lower_bound(self) -> bool:
        """A saturated feed means the true velocity is at least this, possibly far more."""
        return self.saturated


def review_velocity(
    reviews: Sequence[Any],
    *,
    window_days: int = 90,
    now: datetime | None = None,
    min_reviews_for_span: int = 20,
    saturation_span_days: int = 30,
) -> Velocity:
    """Reviews/day over the trailing window.

    Two modes. Once enough history has accumulated we count reviews inside the window and
    divide by the window -- the honest measurement. Before that we approximate from the
    timestamp span of the reviews we have.

    The approximation saturates: the feed only serves ~500 reviews, so a high-volume app's
    500 most recent reviews may span three days, and N/span reports a velocity that is a
    floor rather than a rate. We flag that rather than reporting a wrong number.
    """
    now = now or datetime.now(UTC)
    stamps = _timestamps(reviews)
    if not stamps:
        return Velocity(
            value=None,
            method="none",
            window_days=window_days,
            sample_size=0,
            notes=["no reviews with usable timestamps"],
        )

    cutoff = now - timedelta(days=window_days)
    in_window = [t for t in stamps if t >= cutoff]
    oldest, newest = stamps[0], stamps[-1]
    span_days = (newest - oldest).total_seconds() / 86400.0

    # The feed reaches back past the window: we can see the whole window, so counting
    # inside it is a true measurement rather than an approximation.
    covers_window = oldest <= cutoff
    if covers_window:
        return Velocity(
            value=len(in_window) / window_days,
            method="window",
            window_days=window_days,
            sample_size=len(in_window),
            span_days=round(span_days, 2),
        )

    # Fall back to the span approximation.
    if len(stamps) < min_reviews_for_span:
        return Velocity(
            value=None,
            method="none",
            window_days=window_days,
            sample_size=len(stamps),
            span_days=round(span_days, 2),
            notes=[f"only {len(stamps)} reviews; need {min_reviews_for_span} to approximate"],
        )

    if span_days <= 0:
        # Every review carries the same timestamp -- can't derive a rate.
        return Velocity(
            value=None,
            method="none",
            window_days=window_days,
            sample_size=len(stamps),
            span_days=0.0,
            saturated=True,
            notes=["all reviews share one timestamp; feed is saturated"],
        )

    saturated = span_days < saturation_span_days
    notes = []
    if saturated:
        notes.append(
            f"feed saturated: {len(stamps)} reviews span only {span_days:.1f}d; "
            f"velocity is a lower bound"
        )
    return Velocity(
        value=len(stamps) / span_days,
        method="span",
        window_days=window_days,
        sample_size=len(stamps),
        saturated=saturated,
        span_days=round(span_days, 2),
        notes=notes,
    )


def velocity_trend(
    reviews: Sequence[Any],
    *,
    window_days: int = 90,
    now: datetime | None = None,
) -> float | None:
    """Trailing-window velocity / preceding-window velocity.

    Needs ~2x window_days of review history, so this is null early in the project's life.
    Returns None rather than a misleading number when the feed doesn't reach back far
    enough -- a truncated feed makes the older window look artificially empty, which would
    read as spurious growth.
    """
    now = now or datetime.now(UTC)
    stamps = _timestamps(reviews)
    if not stamps:
        return None

    recent_cutoff = now - timedelta(days=window_days)
    prior_cutoff = now - timedelta(days=2 * window_days)

    # If the feed doesn't reach past the prior window's start, the prior window is
    # truncated and the ratio is meaningless.
    if stamps[0] > prior_cutoff:
        return None

    recent = sum(1 for t in stamps if t >= recent_cutoff)
    prior = sum(1 for t in stamps if prior_cutoff <= t < recent_cutoff)
    if prior == 0:
        return None
    return recent / prior


def staleness_days(
    current_version_release_date: datetime | str | None,
    *,
    now: datetime | None = None,
) -> int | None:
    """Days since the incumbent last shipped."""
    dt = _as_dt(current_version_release_date)
    if dt is None:
        return None
    now = now or datetime.now(UTC)
    return max(0, int((now - dt).total_seconds() // 86400))


def rating_decay(
    average_user_rating: float | None,
    average_user_rating_current_version: float | None,
    *,
    user_rating_count: int | None = None,
    user_rating_count_current_version: int | None = None,
) -> float | None:
    """Lifetime rating minus current-version rating.

    Positive means the current build is rated worse than the app's history. The brief
    calls this the highest-signal free field, and it was -- but as of the 2026-09-10
    probe Apple returns `averageUserRatingForCurrentVersion` exactly equal to
    `averageUserRating` (and the same for the two counts) for every app sampled. The
    store no longer segments ratings by version.

    So we detect the degenerate case and return None -- "not measurable" -- rather than
    0.0. That distinction matters: a hard 0.0 would score as real evidence of a healthy
    incumbent for every app on the store, and it would do so at the heaviest weight in
    the rubric. Returning None lets the scorer renormalise onto the signals that
    survive. See `review_rating_decay` for the live substitute, and docs/endpoints.md.
    """
    if average_user_rating is None or average_user_rating_current_version is None:
        return None
    counts_identical = (
        user_rating_count is not None
        and user_rating_count_current_version is not None
        and user_rating_count == user_rating_count_current_version
    )
    if counts_identical and average_user_rating == average_user_rating_current_version:
        # Apple is echoing the lifetime figures, not reporting a current-version one.
        return None
    return average_user_rating - average_user_rating_current_version


def review_rating_decay(
    reviews: Sequence[Any],
    lifetime_rating: float | None,
    *,
    window_days: int = 90,
    now: datetime | None = None,
    min_reviews: int = 10,
) -> float | None:
    """Lifetime rating minus the mean rating of recent reviews.

    A live reconstruction of the signal `rating_decay` used to carry: is the incumbent
    rated worse *now* than it was historically? Apple stopped segmenting ratings by
    version, but the reviews feed still carries a per-review star rating with a
    timestamp, so the comparison can be rebuilt from it.

    Read it as a trend, not as the API field's twin: people who bother to write a review
    skew more polarised than people who only tap a star, so this runs positive even for
    healthy apps. Score it against its own breakpoints, never the API field's.
    """
    if lifetime_rating is None:
        return None
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    ratings = [
        int(rt)
        for r in reviews
        if (dt := _as_dt(_get(r, "updated_at"))) is not None
        and dt >= cutoff
        and (rt := _get(r, "rating")) is not None
    ]
    if len(ratings) < min_reviews:
        return None
    return lifetime_rating - (sum(ratings) / len(ratings))


@dataclass(frozen=True)
class InstallEstimate:
    """Order-of-magnitude bucket. Never a number."""

    low: int
    high: int
    assumed_rating_rate: float
    bucket: str

    def __str__(self) -> str:
        return self.bucket


def estimated_installs(
    user_rating_count: int | None, *, rating_rate: float = 0.03
) -> InstallEstimate | None:
    """installs ~= ratings / rating_rate.

    The rate is a guess -- historically cited at 1-3%, likely higher since in-app rating
    prompts became standard. So this returns a bucket with the assumed rate attached, not
    a point estimate, and it carries near-zero weight in the rubric.
    """
    if not user_rating_count or user_rating_count <= 0 or rating_rate <= 0:
        return None
    point = user_rating_count / rating_rate
    # Half-order-of-magnitude bracket around the point estimate.
    low, high = int(point / 3), int(point * 3)
    bucket = f"{_human(low)}-{_human(high)}"
    return InstallEstimate(low=low, high=high, assumed_rating_rate=rating_rate, bucket=bucket)


def _human(n: int) -> str:
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            value = n / limit
            return f"{value:.0f}{suffix}" if value >= 10 else f"{value:.1f}{suffix}"
    return str(n)


@dataclass(frozen=True)
class RevenueSignal:
    model: str  # 'paid' | 'free_with_iap_hint' | 'free' | 'unknown'
    price: float | None
    iap_hints: list[str]
    needs_manual_check: bool


# Public data can't confirm a paywall. These only raise a "go look" flag.
_IAP_MARKERS = (
    "subscription",
    "subscribe",
    "premium",
    "pro version",
    "unlock",
    "in-app purchase",
    "free trial",
    "auto-renew",
    "monthly",
    "yearly",
    "upgrade to",
)


def revenue_signal(price: float | None, description: str | None = None) -> RevenueSignal:
    """Monetization model hint from price plus IAP language in the description.

    Weak from public data. The point is to flag apps needing manual paywall inspection,
    not to guess a revenue number.
    """
    text = (description or "").lower()
    hints = [m for m in _IAP_MARKERS if m in text]

    if price is None:
        return RevenueSignal("unknown", None, hints, needs_manual_check=True)
    if price > 0:
        return RevenueSignal("paid", price, hints, needs_manual_check=bool(hints))
    if hints:
        return RevenueSignal("free_with_iap_hint", 0.0, hints, needs_manual_check=True)
    # A free app with no IAP language is the case most likely to be mislabelled.
    return RevenueSignal("free", 0.0, [], needs_manual_check=True)


def complaint_density(
    reviews: Sequence[Any],
    *,
    window_days: int = 90,
    now: datetime | None = None,
    max_star: int = 2,
) -> float | None:
    """Share of in-window reviews rating <= max_star. Feeds incumbent weakness."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    in_window = [
        r for r in reviews if (dt := _as_dt(_get(r, "updated_at"))) is not None and dt >= cutoff
    ]
    if not in_window:
        return None
    low = sum(1 for r in in_window if (rt := _get(r, "rating")) is not None and int(rt) <= max_star)
    return low / len(in_window)


def version_churn(snapshots: Sequence[Any]) -> int:
    """Distinct versions seen across snapshots. Longitudinal; near-zero early."""
    versions = {v for s in snapshots if (v := _get(s, "version"))}
    return len(versions)


def rating_count_growth(snapshots: Sequence[Any]) -> float | None:
    """Ratings added per day between the first and last snapshot.

    This is the metric the snapshot table exists to enable -- it becomes the ground-truth
    demand signal once enough days accumulate, replacing the review-feed approximation.
    """
    rows = [s for s in snapshots if _get(s, "user_rating_count") is not None]
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    d0, d1 = _as_dt(_get(first, "captured_at")), _as_dt(_get(last, "captured_at"))
    if d0 is None or d1 is None:
        return None
    days = (d1 - d0).total_seconds() / 86400.0
    if days <= 0:
        return None
    delta = int(_get(last, "user_rating_count")) - int(_get(first, "user_rating_count"))
    return delta / days

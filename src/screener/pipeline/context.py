"""Assembles everything filters and scoring need for one app, in one DB pass."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from screener import metrics
from screener.config import Config
from screener.db import Database, parse_iso, utc_now


@dataclass
class AppMetrics:
    track_id: int
    name: str
    seller_name: str | None
    primary_genre: str | None
    store_url: str | None

    version: str | None = None
    staleness_days: int | None = None
    average_user_rating: float | None = None
    average_user_rating_current_version: float | None = None
    user_rating_count: int | None = None
    rating_decay: float | None = None
    review_rating_decay: float | None = None
    price: float | None = None
    formatted_price: str | None = None
    minimum_os_version: str | None = None

    velocity: metrics.Velocity | None = None
    velocity_trend: float | None = None
    complaint_density: float | None = None
    installs: metrics.InstallEstimate | None = None
    revenue: metrics.RevenueSignal | None = None
    rating_count_growth: float | None = None
    version_churn: int = 0
    snapshot_days: int = 0
    review_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Flattened for the scores.metrics_json audit column."""
        return {
            "staleness_days": self.staleness_days,
            "rating_decay": self.rating_decay,
            "review_rating_decay": self.review_rating_decay,
            "average_user_rating": self.average_user_rating,
            "average_user_rating_current_version": self.average_user_rating_current_version,
            "user_rating_count": self.user_rating_count,
            "velocity": None if self.velocity is None else self.velocity.value,
            "velocity_method": None if self.velocity is None else self.velocity.method,
            "velocity_saturated": None if self.velocity is None else self.velocity.saturated,
            "velocity_trend": self.velocity_trend,
            "complaint_density": self.complaint_density,
            "rating_count_growth": self.rating_count_growth,
            "snapshot_days": self.snapshot_days,
            "review_count": self.review_count,
            "installs_bucket": None if self.installs is None else self.installs.bucket,
            "installs_assumed_rate": (
                None if self.installs is None else self.installs.assumed_rating_rate
            ),
            "revenue_model": None if self.revenue is None else self.revenue.model,
            "needs_manual_paywall_check": (
                None if self.revenue is None else self.revenue.needs_manual_check
            ),
        }


def build_metrics(
    db: Database, track_id: int, config: Config, *, now: datetime | None = None
) -> AppMetrics | None:
    now = now or utc_now()
    app = db.get_app(track_id)
    if app is None:
        return None
    snap = db.latest_snapshot(track_id)
    reviews = db.reviews_for(track_id)
    snaps = db.snapshots(track_id)
    mcfg = config.metrics

    m = AppMetrics(
        track_id=track_id,
        name=str(app["name"]),
        seller_name=app["seller_name"],
        primary_genre=app["primary_genre"],
        store_url=app["store_url"],
        review_count=len(reviews),
        snapshot_days=len(snaps),
    )

    if snap is not None:
        m.version = snap["version"]
        m.average_user_rating = snap["average_user_rating"]
        m.average_user_rating_current_version = snap["average_user_rating_current_version"]
        m.user_rating_count = snap["user_rating_count"]
        m.price = snap["price"]
        m.formatted_price = snap["formatted_price"]
        m.minimum_os_version = snap["minimum_os_version"]
        m.staleness_days = metrics.staleness_days(
            parse_iso(snap["current_version_release_date"]), now=now
        )
        m.rating_decay = metrics.rating_decay(
            m.average_user_rating,
            m.average_user_rating_current_version,
            user_rating_count=m.user_rating_count,
            user_rating_count_current_version=snap["user_rating_count_current_version"],
        )
        if m.rating_decay is None and m.average_user_rating is not None:
            m.notes.append(
                "rating_decay unavailable: Apple echoes the lifetime rating into the "
                "current-version field; using review-derived decay instead"
            )
        m.installs = metrics.estimated_installs(m.user_rating_count, rating_rate=mcfg.rating_rate)
        m.revenue = metrics.revenue_signal(m.price, app["description"])
    else:
        m.notes.append("no snapshot yet")

    m.velocity = metrics.review_velocity(
        reviews,
        window_days=mcfg.velocity_window_days,
        now=now,
        min_reviews_for_span=mcfg.min_reviews_for_span_velocity,
        saturation_span_days=mcfg.saturation_span_days,
    )
    m.velocity_trend = metrics.velocity_trend(
        reviews, window_days=mcfg.velocity_window_days, now=now
    )
    m.complaint_density = metrics.complaint_density(
        reviews, window_days=mcfg.velocity_window_days, now=now
    )
    m.review_rating_decay = metrics.review_rating_decay(
        reviews, m.average_user_rating, window_days=mcfg.velocity_window_days, now=now
    )
    m.rating_count_growth = metrics.rating_count_growth(snaps)
    m.version_churn = metrics.version_churn(snaps)
    if m.velocity is not None:
        m.notes.extend(m.velocity.notes)
    return m

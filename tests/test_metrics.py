"""Metrics are pure functions, so these are plain value assertions on a fixed clock."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from screener import metrics
from tests.conftest import NOW, make_reviews


class TestReviewVelocity:
    def test_window_method_when_feed_reaches_past_window(self):
        # 200 reviews over 180 days -> the 90-day window is fully covered.
        reviews = make_reviews(200, span_days=180)
        v = metrics.review_velocity(reviews, window_days=90, now=NOW)
        assert v.method == "window"
        assert not v.saturated
        # ~half the reviews fall in the trailing 90 days.
        assert v.value == pytest.approx(100 / 90, rel=0.1)

    def test_span_method_when_feed_is_truncated(self):
        reviews = make_reviews(100, span_days=45)
        v = metrics.review_velocity(reviews, window_days=90, now=NOW)
        assert v.method == "span"
        assert v.value == pytest.approx(100 / 45, rel=0.05)

    def test_saturation_is_flagged_not_silently_reported(self):
        # 500 reviews spanning 3 days: a high-volume app. The rate is a floor.
        reviews = make_reviews(500, span_days=3)
        v = metrics.review_velocity(reviews, window_days=90, now=NOW, saturation_span_days=30)
        assert v.saturated is True
        assert v.is_lower_bound is True
        assert "saturated" in " ".join(v.notes)

    def test_too_few_reviews_returns_none_not_a_guess(self):
        v = metrics.review_velocity(make_reviews(5, span_days=10), now=NOW, min_reviews_for_span=20)
        assert v.value is None
        assert v.method == "none"

    def test_empty_input(self):
        v = metrics.review_velocity([], now=NOW)
        assert v.value is None and v.sample_size == 0

    def test_identical_timestamps_do_not_divide_by_zero(self):
        reviews = make_reviews(30, span_days=0)
        v = metrics.review_velocity(reviews, now=NOW, min_reviews_for_span=20)
        assert v.value is None
        assert v.saturated is True

    def test_naive_timestamps_are_treated_as_utc(self):
        naive = [{"updated_at": datetime(2026, 9, 1, 12), "rating": 3} for _ in range(30)]
        v = metrics.review_velocity(naive, now=NOW, min_reviews_for_span=20)
        assert v.sample_size == 30  # parsed, not discarded

    def test_iso_string_timestamps_with_offset(self):
        rows = [{"updated_at": "2026-09-09T00:24:02-07:00", "rating": 3}]
        v = metrics.review_velocity(rows, now=NOW, min_reviews_for_span=1)
        assert v.sample_size == 1


class TestVelocityTrend:
    def test_null_when_history_too_short(self):
        # Feed only reaches back 100 days; the preceding window is truncated, so a
        # ratio would read as spurious growth.
        assert metrics.velocity_trend(make_reviews(100, span_days=100), now=NOW) is None

    def test_growth_detected(self):
        recent = make_reviews(60, span_days=89, end=NOW)
        older = make_reviews(20, span_days=89, end=NOW - timedelta(days=90))
        # Extend coverage past the prior window so the trend is computable.
        oldest = [{"updated_at": NOW - timedelta(days=200), "rating": 3}]
        trend = metrics.velocity_trend(recent + older + oldest, window_days=90, now=NOW)
        assert trend is not None and trend > 2.0

    def test_decline_detected(self):
        recent = make_reviews(10, span_days=89, end=NOW)
        older = make_reviews(50, span_days=89, end=NOW - timedelta(days=90))
        oldest = [{"updated_at": NOW - timedelta(days=200), "rating": 3}]
        trend = metrics.velocity_trend(recent + older + oldest, window_days=90, now=NOW)
        assert trend is not None and trend < 0.5

    def test_zero_prior_returns_none_not_infinity(self):
        rows = make_reviews(20, span_days=80, end=NOW) + [
            {"updated_at": NOW - timedelta(days=300), "rating": 3}
        ]
        assert metrics.velocity_trend(rows, window_days=90, now=NOW) is None


class TestStalenessAndDecay:
    def test_staleness_days(self):
        shipped = NOW - timedelta(days=500)
        assert metrics.staleness_days(shipped, now=NOW) == 500

    def test_staleness_parses_apple_offset_string(self):
        assert metrics.staleness_days("2024-01-01T00:00:00-08:00", now=NOW) == 983

    def test_staleness_none_input(self):
        assert metrics.staleness_days(None, now=NOW) is None

    def test_rating_decay_positive_means_degrading(self):
        # Lifetime 4.5, current build 3.0 -> the incumbent is getting worse.
        assert metrics.rating_decay(4.5, 3.0) == pytest.approx(1.5)

    def test_rating_decay_missing_input(self):
        assert metrics.rating_decay(4.5, None) is None
        assert metrics.rating_decay(None, 3.0) is None

    def test_apple_echoing_lifetime_rating_reads_as_unavailable(self):
        # Apple now returns the lifetime figures in the current-version fields for
        # every app sampled. That must read as "not measurable", not as a real 0.0 --
        # a hard zero would score as evidence of a healthy incumbent, store-wide, at
        # the heaviest weight in the rubric.
        assert (
            metrics.rating_decay(
                4.5, 4.5, user_rating_count=1000, user_rating_count_current_version=1000
            )
            is None
        )

    def test_genuine_zero_decay_is_kept_when_counts_differ(self):
        assert (
            metrics.rating_decay(
                4.5, 4.5, user_rating_count=1000, user_rating_count_current_version=40
            )
            == 0.0
        )


class TestReviewRatingDecay:
    def test_recent_reviews_worse_than_lifetime(self):
        reviews = make_reviews(30, span_days=60, rating=2)
        decay = metrics.review_rating_decay(reviews, 4.5, window_days=90, now=NOW)
        assert decay == pytest.approx(2.5)

    def test_recent_reviews_matching_lifetime(self):
        reviews = make_reviews(30, span_days=60, rating=4)
        assert metrics.review_rating_decay(reviews, 4.0, window_days=90, now=NOW) == 0.0

    def test_ignores_reviews_outside_window(self):
        old = make_reviews(30, span_days=10, end=NOW - timedelta(days=300), rating=1)
        assert metrics.review_rating_decay(old, 4.5, window_days=90, now=NOW) is None

    def test_too_few_reviews_returns_none(self):
        reviews = make_reviews(3, span_days=10, rating=1)
        assert metrics.review_rating_decay(reviews, 4.5, now=NOW, min_reviews=10) is None

    def test_no_lifetime_rating(self):
        assert metrics.review_rating_decay(make_reviews(30, span_days=60), None, now=NOW) is None


class TestInstallEstimate:
    def test_returns_bucket_with_assumed_rate_attached(self):
        est = metrics.estimated_installs(30_000, rating_rate=0.03)
        assert est is not None
        assert est.assumed_rating_rate == 0.03
        assert "-" in est.bucket  # a range, never a point estimate
        assert est.low < est.high

    def test_rate_changes_the_estimate(self):
        low_rate = metrics.estimated_installs(30_000, rating_rate=0.01)
        high_rate = metrics.estimated_installs(30_000, rating_rate=0.10)
        assert low_rate.low > high_rate.low

    def test_zero_and_none(self):
        assert metrics.estimated_installs(0) is None
        assert metrics.estimated_installs(None) is None


class TestRevenueSignal:
    def test_paid_app(self):
        sig = metrics.revenue_signal(4.99, "A simple one-time purchase app.")
        assert sig.model == "paid" and sig.price == 4.99

    def test_free_with_iap_language_flags_for_manual_check(self):
        sig = metrics.revenue_signal(0.0, "Upgrade to premium with a monthly subscription!")
        assert sig.model == "free_with_iap_hint"
        assert sig.needs_manual_check
        assert "subscription" in sig.iap_hints

    def test_free_app_still_needs_manual_check(self):
        # Public data can't prove the absence of a paywall.
        sig = metrics.revenue_signal(0.0, "A free utility.")
        assert sig.model == "free" and sig.needs_manual_check

    def test_unknown_price(self):
        assert metrics.revenue_signal(None, None).model == "unknown"


class TestComplaintDensity:
    def test_share_of_low_star_reviews(self):
        rows = make_reviews(10, span_days=30, rating=1) + make_reviews(10, span_days=30, rating=5)
        assert metrics.complaint_density(rows, window_days=90, now=NOW) == pytest.approx(0.5)

    def test_ignores_reviews_outside_window(self):
        old = make_reviews(10, span_days=5, end=NOW - timedelta(days=300), rating=1)
        assert metrics.complaint_density(old, window_days=90, now=NOW) is None

    def test_empty(self):
        assert metrics.complaint_density([], now=NOW) is None


class TestSnapshotDerived:
    def test_version_churn_counts_distinct_versions(self):
        snaps = [{"version": "1.0"}, {"version": "1.0"}, {"version": "1.1"}]
        assert metrics.version_churn(snaps) == 2

    def test_rating_count_growth(self):
        snaps = [
            {"captured_at": NOW - timedelta(days=10), "user_rating_count": 1000},
            {"captured_at": NOW, "user_rating_count": 1100},
        ]
        assert metrics.rating_count_growth(snaps) == pytest.approx(10.0)

    def test_growth_needs_two_snapshots(self):
        assert metrics.rating_count_growth([{"captured_at": NOW, "user_rating_count": 5}]) is None

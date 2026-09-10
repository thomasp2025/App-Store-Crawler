"""Filters, scoring and the manual two-pass flow."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from screener.config import ConfigError, ScoringConfig, load_config
from screener.pipeline.context import build_metrics
from screener.pipeline.filters import apply_hard_filters
from screener.pipeline.scoring import (
    bucket_score,
    composite_score,
    import_manual_scores,
    review_queue_rows,
    run_auto_scoring,
    score_app,
)
from screener.sources.models import AppRecord, ReviewRecord

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def seed_app(
    db,
    *,
    track_id=1,
    stale_days=600,
    rating=3.5,
    current_rating=2.5,
    rating_count=5000,
    reviews=0,
    review_rating=1,
):
    app = AppRecord.model_validate(
        {
            "trackId": track_id,
            "trackName": f"App {track_id}",
            "sellerName": "Seller",
            "primaryGenreName": "Utilities",
            "version": "1.0",
            "currentVersionReleaseDate": (NOW - timedelta(days=stale_days)).isoformat(),
            "releaseDate": (NOW - timedelta(days=2000)).isoformat(),
            "userRatingCount": rating_count,
            "averageUserRating": rating,
            "averageUserRatingForCurrentVersion": current_rating,
            "price": 0.0,
            "description": "An app.",
        }
    )
    db.upsert_app(app)
    db.upsert_snapshot(app, captured_at=NOW)
    if reviews:
        step = timedelta(days=80) / max(reviews - 1, 1)
        db.upsert_reviews(
            [
                ReviewRecord(
                    review_id=f"{track_id}-r{i}",
                    track_id=track_id,
                    rating=review_rating,
                    title="t",
                    body="b",
                    app_version="1.0",
                    author="a",
                    updated_at=NOW - step * i,
                )
                for i in range(reviews)
            ]
        )
    return app


class TestBucketScore:
    def test_maps_onto_zero_to_five(self):
        bps = [0.1, 0.5, 2.0, 8.0, 25.0]
        assert bucket_score(0.05, bps) == 0
        assert bucket_score(0.1, bps) == 1
        assert bucket_score(3.0, bps) == 3
        assert bucket_score(1000, bps) == 5

    def test_none_passes_through(self):
        assert bucket_score(None, [1, 2, 3, 4, 5]) is None


class TestCompositeScore:
    def test_renormalises_over_present_dimensions(self):
        weights = {"a": 0.5, "b": 0.5}
        # Only 'a' present -> composite is just 'a', not halved.
        assert composite_score({"a": 4.0, "b": None}, weights) == pytest.approx(4.0)

    def test_weighted_mean(self):
        weights = {"a": 0.75, "b": 0.25}
        assert composite_score({"a": 4.0, "b": 0.0}, weights) == pytest.approx(3.0)

    def test_all_missing(self):
        assert composite_score({"a": None}, {"a": 1.0}) is None


class TestHardFilters:
    def test_fresh_app_is_rejected(self, db, config):
        seed_app(db, stale_days=30)
        m = build_metrics(db, 1, config, now=NOW)
        verdict = apply_hard_filters(m, config)
        assert not verdict.passed
        assert any("staleness" in r for r in verdict.reasons)

    def test_happy_users_rejected(self, db, config):
        seed_app(db, rating=4.8)
        verdict = apply_hard_filters(build_metrics(db, 1, config, now=NOW), config)
        assert any("users satisfied" in r for r in verdict.reasons)

    def test_broken_market_rejected(self, db, config):
        seed_app(db, rating=2.0)
        verdict = apply_hard_filters(build_metrics(db, 1, config, now=NOW), config)
        assert any("broken market" in r for r in verdict.reasons)

    def test_too_few_ratings_rejected(self, db, config):
        seed_app(db, rating_count=50)
        verdict = apply_hard_filters(build_metrics(db, 1, config, now=NOW), config)
        assert any("prove demand" in r for r in verdict.reasons)

    def test_stale_unhappy_popular_app_passes(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, current_rating=2.2, rating_count=9000)
        verdict = apply_hard_filters(build_metrics(db, 1, config, now=NOW), config)
        assert verdict.passed, verdict.reasons

    def test_missing_data_skips_rather_than_rejects(self, db, config):
        # Early on, velocity_trend is null for everything. Rejecting on absent data
        # would empty the funnel for reasons unrelated to the apps.
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        verdict = apply_hard_filters(build_metrics(db, 1, config, now=NOW), config)
        assert "velocity_trend" in verdict.skipped
        assert verdict.passed

    def test_fresh_competitors_reject(self, db, config):
        seed_app(db, track_id=1, stale_days=900, rating=3.4, rating_count=9000)
        seed_app(db, track_id=2, stale_days=10)
        seed_app(db, track_id=3, stale_days=20)
        kid = db.upsert_keyword("row counter")
        db.record_hits(kid, [(1, 1), (2, 2), (3, 3)])
        m = build_metrics(db, 1, config, now=NOW)
        verdict = apply_hard_filters(m, config, db=db)
        assert any("actively served" in r for r in verdict.reasons)


class TestScoring:
    def test_auto_dimensions_computed_manual_left_null(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, current_rating=2.0, rating_count=9000, reviews=60)
        result, m, _ = score_app(db, 1, config, now=NOW)
        assert result.dimensions["demand_persistence"] is not None
        assert result.dimensions["incumbent_weakness"] is not None
        assert result.is_provisional
        assert set(result.manual_missing) == set(ScoringConfig.MANUAL_DIMENSIONS)

    def test_rating_decay_drives_incumbent_weakness(self, db, config):
        seed_app(db, track_id=1, stale_days=900, rating=3.4, current_rating=3.4, rating_count=9000)
        seed_app(db, track_id=2, stale_days=900, rating=3.4, current_rating=1.8, rating_count=9000)
        weak_no_decay = score_app(db, 1, config, now=NOW)[0].dimensions["incumbent_weakness"]
        weak_decay = score_app(db, 2, config, now=NOW)[0].dimensions["incumbent_weakness"]
        assert weak_decay > weak_no_decay

    def test_kill_flag_overrides_a_good_composite(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, current_rating=2.0, rating_count=9000, reviews=60)
        db.set_kill_flag(1, "native_os_feature", True)
        result, _, _ = score_app(db, 1, config, now=NOW)
        assert not result.passed_filters
        assert result.kill_reason.startswith("kill:")

    def test_scores_are_versioned_and_persisted(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        run_auto_scoring(db, config, now=NOW)
        row = db.latest_score_for(1, config.scoring.rubric_version)
        assert row is not None
        assert row["rubric_version"] == config.scoring.rubric_version
        assert row["metrics_json"]  # audit trail of the inputs

    def test_rescoring_does_not_destroy_history(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        run_auto_scoring(db, config, now=NOW)
        run_auto_scoring(db, config, now=NOW + timedelta(days=1))
        rows = db.conn.execute("SELECT COUNT(*) n FROM scores WHERE track_id=1").fetchone()
        assert rows["n"] == 2


class TestManualFlow:
    def test_queue_lists_only_passing_unscored_apps(self, db, config):
        seed_app(db, track_id=1, stale_days=900, rating=3.4, rating_count=9000)
        seed_app(db, track_id=2, stale_days=10)  # rejected: too fresh
        rows = review_queue_rows(db, config)
        assert [r["track_id"] for r in rows] == [1]
        # Blank columns for the human to fill.
        assert rows[0]["monetization_ceiling"] == ""

    def test_import_merges_manual_scores(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        count = import_manual_scores(
            db,
            [
                {
                    "track_id": "1",
                    "monetization_ceiling": "4",
                    "capability_delta": "5",
                    "build_cost": "3",
                    "distribution_wedge": "2",
                    "notes": "looks good",
                }
            ],
            config,
        )
        assert count == 1
        result, _, _ = score_app(db, 1, config, now=NOW)
        assert result.dimensions["monetization_ceiling"] == 4.0
        assert not result.is_provisional

    def test_import_ignores_out_of_range_and_garbage(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        import_manual_scores(
            db,
            [
                {
                    "track_id": "1",
                    "monetization_ceiling": "99",
                    "capability_delta": "abc",
                    "build_cost": "",
                    "distribution_wedge": "2",
                }
            ],
            config,
        )
        row = db.manual_score(1, config.scoring.rubric_version)
        assert row["monetization_ceiling"] is None
        assert row["capability_delta"] is None
        assert row["distribution_wedge"] == 2.0

    def test_blank_cells_do_not_clobber_existing_scores(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        import_manual_scores(db, [{"track_id": "1", "monetization_ceiling": "4"}], config)
        import_manual_scores(db, [{"track_id": "1", "capability_delta": "5"}], config)
        row = db.manual_score(1, config.scoring.rubric_version)
        assert row["monetization_ceiling"] == 4.0 and row["capability_delta"] == 5.0

    def test_kill_flags_ride_along_in_csv(self, db, config):
        seed_app(db, stale_days=900, rating=3.4, rating_count=9000)
        import_manual_scores(db, [{"track_id": "1", "native_os_feature": "yes"}], config)
        assert db.kill_flags(1)["native_os_feature"] is True


class TestConfigValidation:
    def test_weights_must_sum_to_one(self, tmp_path):
        broken = tmp_path / "config.toml"
        broken.write_text(
            pathlib.Path("config.toml")
            .read_text()
            .replace("monetization_ceiling = 0.25", "monetization_ceiling = 0.45")
        )
        with pytest.raises(ConfigError, match="sum to 1.0"):
            load_config(broken)

    def test_breakpoints_must_be_ascending(self, tmp_path):
        broken = tmp_path / "config.toml"
        broken.write_text(
            pathlib.Path("config.toml")
            .read_text()
            .replace(
                "velocity_breakpoints = [0.1, 0.5, 2.0, 8.0, 25.0]",
                "velocity_breakpoints = [0.1, 5.0, 2.0, 8.0, 25.0]",
            )
        )
        with pytest.raises(ConfigError, match="ascending"):
            load_config(broken)

    def test_valid_config_loads(self, config):
        assert config.scoring.rubric_version
        assert abs(sum(config.scoring.weights.values()) - 1.0) < 1e-9

"""The two things the brief says to get right: snapshot idempotency and review dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from screener.sources.models import AppRecord, ReviewRecord

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _app(track_id=1, **kw) -> AppRecord:
    return AppRecord.model_validate(
        {
            "trackId": track_id,
            "trackName": "Test App",
            "userRatingCount": 500,
            "averageUserRating": 3.5,
            "version": "1.0",
            **kw,
        }
    )


def _review(rid="r1", body="original", rating=1, track_id=1, when=NOW) -> ReviewRecord:
    return ReviewRecord(
        review_id=rid,
        track_id=track_id,
        rating=rating,
        title="t",
        body=body,
        app_version="1.0",
        author="a",
        updated_at=when,
    )


class TestSnapshotIdempotency:
    def test_same_day_rerun_updates_rather_than_duplicates(self, db):
        app = _app()
        db.upsert_app(app)
        assert db.upsert_snapshot(app, captured_at=NOW) is True
        # Re-running collection later the same day must not add a second row.
        assert db.upsert_snapshot(app, captured_at=NOW + timedelta(hours=6)) is False
        assert len(db.snapshots(1)) == 1

    def test_different_days_create_separate_rows(self, db):
        app = _app()
        db.upsert_app(app)
        db.upsert_snapshot(app, captured_at=NOW)
        db.upsert_snapshot(app, captured_at=NOW + timedelta(days=1))
        assert len(db.snapshots(1)) == 2

    def test_rerun_refreshes_values(self, db):
        db.upsert_app(_app())
        db.upsert_snapshot(_app(userRatingCount=500), captured_at=NOW)
        db.upsert_snapshot(_app(userRatingCount=600), captured_at=NOW)
        assert db.latest_snapshot(1)["user_rating_count"] == 600

    def test_day_boundary_uses_utc_not_local(self, db):
        db.upsert_app(_app())
        # 23:00 UTC and 01:00 UTC next day are different days.
        db.upsert_snapshot(_app(), captured_at=datetime(2026, 9, 10, 23, 0, tzinfo=UTC))
        db.upsert_snapshot(_app(), captured_at=datetime(2026, 9, 11, 1, 0, tzinfo=UTC))
        assert len(db.snapshots(1)) == 2


class TestReviewDedup:
    def test_new_reviews_insert(self, db):
        db.upsert_app(_app())
        inserted, updated = db.upsert_reviews([_review("r1"), _review("r2")])
        assert (inserted, updated) == (2, 0)

    def test_repeated_page_overlap_does_not_duplicate(self, db):
        db.upsert_app(_app())
        db.upsert_reviews([_review("r1")])
        inserted, updated = db.upsert_reviews([_review("r1")])
        assert (inserted, updated) == (0, 0)
        assert db.review_count(1) == 1

    def test_edited_review_updates_in_place(self, db):
        db.upsert_app(_app())
        db.upsert_reviews([_review("r1", body="original")])
        inserted, updated = db.upsert_reviews([_review("r1", body="edited!")])
        assert (inserted, updated) == (0, 1)
        assert db.review_count(1) == 1
        row = db.reviews_for(1)[0]
        assert row["body"] == "edited!"
        assert row["revised_at"] is not None

    def test_rating_change_counts_as_edit(self, db):
        db.upsert_app(_app())
        db.upsert_reviews([_review("r1", rating=1)])
        _, updated = db.upsert_reviews([_review("r1", rating=5)])
        assert updated == 1

    def test_filters_by_window_and_rating(self, db):
        db.upsert_app(_app())
        db.upsert_reviews(
            [
                _review("r1", rating=1, when=NOW),
                _review("r2", rating=5, when=NOW),
                _review("r3", rating=1, when=NOW - timedelta(days=400)),
            ]
        )
        recent_negative = db.reviews_for(1, since=NOW - timedelta(days=365), max_rating=2)
        assert [r["review_id"] for r in recent_negative] == ["r1"]


class TestAppsAndKeywords:
    def test_upsert_app_preserves_first_seen(self, db):
        db.upsert_app(_app())
        first = db.get_app(1)["first_seen_at"]
        db.upsert_app(_app(trackName="Renamed"))
        row = db.get_app(1)
        assert row["first_seen_at"] == first
        assert row["name"] == "Renamed"

    def test_keyword_cohort_finds_co_discovered_apps(self, db):
        for tid in (1, 2, 3):
            db.upsert_app(_app(track_id=tid))
        kid = db.upsert_keyword("row counter")
        db.record_hits(kid, [(1, 1), (2, 2), (3, 3)])
        assert sorted(db.keyword_cohort(1)) == [2, 3]

    def test_keyword_upsert_is_stable(self, db):
        assert db.upsert_keyword("term") == db.upsert_keyword("term")

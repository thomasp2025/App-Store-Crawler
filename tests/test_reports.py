"""Report rendering and an end-to-end pipeline pass over fixture data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from screener import reports
from screener.db import Database
from screener.http.cache import DiskCache
from screener.http.client import ITunesClient
from screener.pipeline import discovery, scoring, snapshot
from screener.pipeline import reviews as reviews_stage
from screener.sources.itunes import ITunesSource
from screener.sources.models import AppRecord, ReviewRecord

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class TestSparkline:
    def test_renders_a_series(self):
        assert len(reports.sparkline([1, 2, 3, 4])) == 4

    def test_gaps_render_as_space(self):
        assert " " in reports.sparkline([1, None, 3])

    def test_flat_series_does_not_divide_by_zero(self):
        assert reports.sparkline([5, 5, 5]) == "▁▁▁"

    def test_empty(self):
        assert reports.sparkline([]) == ""
        assert reports.sparkline([None, None]) == ""


class TestReports:
    def test_empty_shortlist_explains_itself(self, db, config):
        text = reports.shortlist_markdown(db, config)
        # A new user's first run: say why it's empty rather than showing a bare table.
        assert "Nothing has cleared the filters yet" in text

    def test_shortlist_lists_passing_apps(self, db, config):
        app = AppRecord.model_validate(
            {
                "trackId": 1,
                "trackName": "Row Counter",
                "sellerName": "Someone",
                "primaryGenreName": "Utilities",
                "currentVersionReleaseDate": (NOW - timedelta(days=900)).isoformat(),
                "userRatingCount": 9000,
                "averageUserRating": 3.4,
                "averageUserRatingForCurrentVersion": 2.1,
            }
        )
        db.upsert_app(app)
        db.upsert_snapshot(app, captured_at=NOW)
        scoring.run_auto_scoring(db, config, now=NOW)
        text = reports.shortlist_markdown(db, config)
        assert "Row Counter" in text
        assert "needs manual scoring" in text

    def test_dossier_for_unknown_app(self, db, config):
        assert "No app with track_id" in reports.app_dossier(db, 999, config)

    def test_dossier_renders_metrics_and_caveats(self, db, config):
        app = AppRecord.model_validate(
            {
                "trackId": 1,
                "trackName": "Row Counter",
                "sellerName": "Someone",
                "currentVersionReleaseDate": (NOW - timedelta(days=900)).isoformat(),
                "userRatingCount": 9000,
                "averageUserRating": 3.4,
                "averageUserRatingForCurrentVersion": 2.1,
                "price": 0.0,
                "description": "Subscribe to premium for more rows.",
            }
        )
        db.upsert_app(app)
        db.upsert_snapshot(app, captured_at=NOW)
        db.upsert_reviews(
            [
                ReviewRecord(
                    review_id=f"r{i}",
                    track_id=1,
                    rating=1,
                    title="Broken",
                    body="Crashes every time I open it.",
                    app_version="1.0",
                    author="a",
                    updated_at=NOW - timedelta(days=i),
                )
                for i in range(30)
            ]
        )
        text = reports.app_dossier(db, 1, config)
        assert "Rating decay" in text
        assert "+1.30" in text  # 3.4 - 2.1
        assert "inspect paywall manually" in text  # IAP language found
        assert "Complaint clusters" in text

    def test_keyword_yield_report(self, db, config):
        db.upsert_keyword("row counter")
        assert "row counter" in reports.keyword_yield_markdown(db)

    def test_csv_roundtrip(self):
        text = reports.rows_to_csv([{"a": 1, "b": "x"}])
        assert text.splitlines()[0] == "a,b"
        assert reports.rows_to_csv([]) == ""


class TestEndToEnd:
    @respx.mock
    async def test_discover_snapshot_reviews_score(
        self, tmp_path, config, search_payload, lookup_payload, reviews_payload
    ):
        """Full pipeline against recorded fixtures, no network."""
        respx.get(url__startswith="https://itunes.apple.com/search").mock(
            return_value=httpx.Response(200, text=json.dumps(search_payload))
        )
        respx.get(url__startswith="https://itunes.apple.com/lookup").mock(
            return_value=httpx.Response(200, text=json.dumps(lookup_payload))
        )
        respx.get(url__regex=r".*customerreviews.*").mock(
            return_value=httpx.Response(200, text=json.dumps(reviews_payload))
        )

        config.discovery.__dict__["seeds_file"] = tmp_path / "seeds.txt"
        (tmp_path / "seeds.txt").write_text("knitting row counter\n")

        db = Database(tmp_path / "e2e.db")
        db.migrate()
        client = ITunesClient(config, cache=DiskCache(tmp_path / "c", enabled=False))
        source = ITunesSource(client, config)

        disc = await discovery.run_discovery(db, source, config, use_cache=False)
        assert disc.terms_searched == 1
        assert disc.new_apps == len(search_payload["results"])

        # Every discovered app already has a snapshot -- time series starts on day one.
        assert db.latest_snapshot(disc_first := db.all_track_ids()[0]) is not None

        snap = await snapshot.run_snapshot(db, source, config, use_cache=False)
        assert snap.captured == 2  # the lookup fixture holds two records

        ing = await reviews_stage.run_review_ingest(
            db, source, config, track_ids=[320606217], use_cache=False
        )
        assert ing.inserted == len(reviews_payload["feed"]["entry"])

        # Re-running the whole thing must not duplicate anything.
        before = db.counts()
        await snapshot.run_snapshot(db, source, config, use_cache=False)
        await reviews_stage.run_review_ingest(
            db, source, config, track_ids=[320606217], use_cache=False
        )
        after = db.counts()
        assert after["app_snapshots"] == before["app_snapshots"]
        assert after["reviews"] == before["reviews"]

        result = scoring.run_auto_scoring(db, config)
        assert result.scored > 0
        assert reports.shortlist_markdown(db, config)
        assert reports.app_dossier(db, disc_first, config)

        await client.aclose()
        db.close()

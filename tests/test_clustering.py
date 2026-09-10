"""Clustering, with a stub client. No API calls, no key needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from screener.pipeline.clustering import ClusterExtraction, ComplaintTheme, cluster_app
from screener.sources.models import AppRecord, ReviewRecord

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class StubClient:
    """Returns canned extractions and records the prompts it was given."""

    def __init__(self, themes_per_call: list[list[ComplaintTheme]]):
        self._queue = list(themes_per_call)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        themes = self._queue.pop(0) if self._queue else []
        return SimpleNamespace(
            parsed_output=ClusterExtraction(themes=themes),
            to_json=lambda: '{"stub": true}',
        )


def theme(name="Crashes on launch", kind="broken", ids=("1-r0",), freq=3):
    return ComplaintTheme(
        theme=name,
        kind=kind,
        frequency=freq,
        severity=4.0,
        example_review_ids=list(ids),
        summary="Users report crashes.",
    )


def seed(db, *, negative=5, positive=0, old=0):
    app = AppRecord.model_validate({"trackId": 1, "trackName": "Test App"})
    db.upsert_app(app)
    db.upsert_snapshot(app, captured_at=NOW)
    rows = []
    for i in range(negative):
        rows.append(
            ReviewRecord(
                review_id=f"1-r{i}",
                track_id=1,
                rating=1,
                title="bad",
                body="it crashes",
                app_version="1.0",
                author="a",
                updated_at=NOW - timedelta(days=i),
            )
        )
    for i in range(positive):
        rows.append(
            ReviewRecord(
                review_id=f"1-p{i}",
                track_id=1,
                rating=5,
                title="good",
                body="love it",
                app_version="1.0",
                author="a",
                updated_at=NOW - timedelta(days=i),
            )
        )
    for i in range(old):
        rows.append(
            ReviewRecord(
                review_id=f"1-o{i}",
                track_id=1,
                rating=1,
                title="old",
                body="stale complaint",
                app_version="0.9",
                author="a",
                updated_at=NOW - timedelta(days=500 + i),
            )
        )
    db.upsert_reviews(rows)


class TestClusterApp:
    def test_extracts_and_persists_themes(self, db, config):
        seed(db)
        client = StubClient([[theme()]])
        result = cluster_app(db, 1, config, client=client)
        assert result.error is None
        assert len(result.themes) == 1
        stored = db.latest_clusters(1)
        assert stored[0]["theme"] == "Crashes on launch"
        assert stored[0]["kind"] == "broken"

    def test_only_negative_reviews_are_sent(self, db, config):
        seed(db, negative=3, positive=10)
        client = StubClient([[theme()]])
        cluster_app(db, 1, config, client=client)
        prompt = client.calls[0]["messages"][0]["content"]
        assert "love it" not in prompt  # 5-star reviews excluded
        assert "it crashes" in prompt

    def test_reviews_outside_lookback_excluded(self, db, config):
        seed(db, negative=3, old=5)
        client = StubClient([[theme()]])
        result = cluster_app(db, 1, config, client=client)
        assert result.review_count == 3
        assert "stale complaint" not in client.calls[0]["messages"][0]["content"]

    def test_invented_review_ids_are_dropped(self, db, config):
        seed(db, negative=3)
        client = StubClient([[theme(ids=("1-r0", "not-a-real-id"))]])
        result = cluster_app(db, 1, config, client=client)
        assert result.dropped_ids == 1
        assert result.themes[0]["example_review_ids"] == ["1-r0"]

    def test_theme_with_no_valid_evidence_is_discarded(self, db, config):
        # An unverifiable theme is worse than a missing one -- it reads as evidence.
        seed(db, negative=3)
        client = StubClient([[theme(ids=("fabricated",))]])
        result = cluster_app(db, 1, config, client=client)
        assert result.themes == []

    def test_batches_are_chunked_and_merged_not_truncated(self, db, config):
        seed(db, negative=10)
        config.clustering.__dict__["max_reviews_per_batch"] = 4
        merged = [theme(name="Merged theme", ids=("1-r0", "1-r5"), freq=9)]
        client = StubClient([[theme()], [theme()], [theme()], merged])
        result = cluster_app(db, 1, config, client=client)
        assert result.batch_count == 3  # 4 + 4 + 2, nothing dropped
        assert len(client.calls) == 4  # 3 extractions + 1 merge pass
        assert result.themes[0]["theme"] == "Merged theme"

    def test_broken_vs_missing_feature_preserved(self, db, config):
        seed(db, negative=4)
        client = StubClient(
            [
                [
                    theme(name="Crashes", kind="broken", ids=("1-r0",)),
                    theme(name="No dark mode", kind="missing_feature", ids=("1-r1",)),
                ]
            ]
        )
        result = cluster_app(db, 1, config, client=client)
        kinds = {t["theme"]: t["kind"] for t in result.themes}
        assert kinds == {"Crashes": "broken", "No dark mode": "missing_feature"}

    def test_no_negative_reviews_is_a_clean_skip(self, db, config):
        seed(db, negative=0, positive=5)
        result = cluster_app(db, 1, config, client=StubClient([]))
        assert result.themes == []
        assert "no negative reviews" in result.error

    def test_api_failure_is_recorded_not_raised(self, db, config):
        seed(db, negative=3)

        class Boom:
            def __init__(self):
                self.messages = SimpleNamespace(parse=self._raise)

            def _raise(self, **kwargs):
                raise RuntimeError("api exploded")

        result = cluster_app(db, 1, config, client=Boom())
        assert result.error is not None and "api exploded" in result.error
        run = db.conn.execute("SELECT * FROM clustering_runs WHERE track_id=1").fetchone()
        assert run["error"] is not None

    def test_raw_output_persisted_for_debugging(self, db, config):
        seed(db, negative=3)
        cluster_app(db, 1, config, client=StubClient([[theme()]]))
        run = db.conn.execute("SELECT * FROM clustering_runs WHERE track_id=1").fetchone()
        assert run["raw_output"]
        assert run["model"] == config.clustering.model

    def test_uses_configured_model(self, db, config):
        seed(db, negative=3)
        client = StubClient([[theme()]])
        cluster_app(db, 1, config, client=client)
        assert client.calls[0]["model"] == config.clustering.model
        # Opus 5 rejects sampling params -- we must never send one.
        assert "temperature" not in client.calls[0]

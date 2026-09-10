"""Apple's JSON is inconsistent; these lock in the shapes we actually saw."""

from __future__ import annotations

from datetime import UTC, datetime

from screener.sources.models import (
    AppRecord,
    ReviewRecord,
    SearchResponse,
    entries_from_feed,
    parse_apple_datetime,
)


class TestAppleDatetime:
    def test_offset_is_normalised_to_utc(self):
        dt = parse_apple_datetime("2026-09-09T00:24:02-07:00")
        assert dt == datetime(2026, 9, 9, 7, 24, 2, tzinfo=UTC)
        assert dt.tzinfo is not None

    def test_z_suffix(self):
        assert parse_apple_datetime("2026-01-01T00:00:00Z").tzinfo is UTC

    def test_naive_assumed_utc_not_host_local(self):
        # Adopting the crawler host's zone here would corrupt velocity silently.
        assert parse_apple_datetime("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)

    def test_garbage_and_empty(self):
        assert parse_apple_datetime("not a date") is None
        assert parse_apple_datetime("") is None
        assert parse_apple_datetime(None) is None


class TestAppRecord:
    def test_parses_real_lookup_response(self, lookup_payload):
        app = AppRecord.model_validate(lookup_payload["results"][0])
        assert app.track_id > 0
        assert app.track_name
        assert app.current_version_release_date.tzinfo is not None
        assert app.store_url.startswith("http")

    def test_parses_real_search_response(self, search_payload):
        parsed = SearchResponse.model_validate(search_payload)
        assert parsed.result_count == len(parsed.results)
        assert all(a.track_id for a in parsed.results)

    def test_missing_fields_do_not_raise(self):
        # Apple drops fields without warning; only identity is required.
        app = AppRecord.model_validate({"trackId": 1, "trackName": "X"})
        assert app.user_rating_count is None
        assert app.average_user_rating is None
        assert app.genres == []

    def test_numeric_strings_are_coerced(self):
        app = AppRecord.model_validate(
            {
                "trackId": 1,
                "trackName": "X",
                "price": "4.99",
                "userRatingCount": "1234",
                "averageUserRating": "4.5",
            }
        )
        assert app.price == 4.99
        assert app.user_rating_count == 1234
        assert app.average_user_rating == 4.5

    def test_unparseable_numbers_become_none_not_crash(self):
        app = AppRecord.model_validate({"trackId": 1, "trackName": "X", "price": "free"})
        assert app.price is None


class TestReviewRecord:
    def test_parses_real_feed_entry(self, reviews_payload):
        entries = entries_from_feed(reviews_payload)
        assert entries
        review = ReviewRecord.from_feed_entry(entries[0], track_id=320606217)
        assert review is not None
        assert 1 <= review.rating <= 5
        assert review.updated_at.tzinfo is not None
        assert review.review_id

    def test_entry_without_rating_is_skipped(self):
        # Page 1's first entry is sometimes app metadata, not a review.
        entry = {
            "id": {"label": "1"},
            "updated": {"label": "2026-01-01T00:00:00-07:00"},
            "im:name": {"label": "Some App"},
        }
        assert ReviewRecord.from_feed_entry(entry, 1) is None

    def test_single_entry_dict_not_list(self):
        # A feed with exactly one review returns a dict, not a list.
        payload = {"feed": {"entry": {"id": {"label": "1"}}}}
        assert len(entries_from_feed(payload)) == 1

    def test_feed_with_no_entries(self):
        assert entries_from_feed({"feed": {}}) == []
        assert entries_from_feed({}) == []

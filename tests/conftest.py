"""Shared fixtures. No test in this suite makes a network call."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from screener.config import load_config
from screener.db import Database
from screener.sources.models import AppRecord

FIXTURES = Path(__file__).parent / "fixtures"

# Fixed clock so velocity/staleness assertions never drift with the calendar.
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


@pytest.fixture
def config():
    return load_config(Path(__file__).parent.parent / "config.toml")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def search_payload():
    return load_fixture("search_knitting.json")


@pytest.fixture
def lookup_payload():
    return load_fixture("lookup_two.json")


@pytest.fixture
def reviews_payload():
    return load_fixture("reviews_page1.json")


@pytest.fixture
def sample_app(lookup_payload) -> AppRecord:
    return AppRecord.model_validate(lookup_payload["results"][0])


def make_reviews(
    count: int, *, span_days: float, end: datetime = NOW, rating: int = 3, track_id: int = 1
) -> list[dict]:
    """Synthesise reviews evenly spaced backwards from `end`."""
    step = timedelta(0) if count == 1 else timedelta(days=span_days) / (count - 1)
    return [
        {
            "review_id": f"r{i}",
            "track_id": track_id,
            "rating": rating,
            "title": f"t{i}",
            "body": f"b{i}",
            "app_version": "1.0",
            "author": "a",
            "updated_at": end - step * i,
        }
        for i in range(count)
    ]

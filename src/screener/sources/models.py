"""Pydantic models for Apple's responses.

Apple's JSON is inconsistent: fields vanish without warning, numbers arrive as strings,
and the reviews feed nests every scalar under a "label" key. Everything from the network
is validated through here so a shape change surfaces as a validation error at the edge
rather than a None deep inside a metric.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def parse_apple_datetime(value: str | datetime | None) -> datetime | None:
    """Parse Apple's ISO-8601 and normalise to UTC-aware.

    Apple returns offsets like `2026-09-09T00:24:02-07:00` and, on the lookup endpoint,
    `...Z`. Naive datetimes break velocity math silently, so everything is coerced to
    aware UTC or rejected.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Apple has never sent a naive timestamp, but if it does, assume UTC rather than
        # silently adopting the crawler host's local zone.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _unlabel(value: Any) -> str | None:
    """Reviews feed wraps every scalar as {"label": "..."}. Unwrap defensively."""
    if isinstance(value, dict):
        label = value.get("label")
        return str(label) if label is not None else None
    if value is None:
        return None
    return str(value)


class AppRecord(BaseModel):
    """One app as returned by Search or Lookup. Same shape from both endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    track_id: int = Field(alias="trackId")
    bundle_id: str | None = Field(default=None, alias="bundleId")
    track_name: str = Field(alias="trackName")
    seller_name: str | None = Field(default=None, alias="sellerName")

    current_version_release_date: datetime | None = Field(
        default=None, alias="currentVersionReleaseDate"
    )
    release_date: datetime | None = Field(default=None, alias="releaseDate")
    version: str | None = None

    user_rating_count: int | None = Field(default=None, alias="userRatingCount")
    average_user_rating: float | None = Field(default=None, alias="averageUserRating")
    user_rating_count_current_version: int | None = Field(
        default=None, alias="userRatingCountForCurrentVersion"
    )
    average_user_rating_current_version: float | None = Field(
        default=None, alias="averageUserRatingForCurrentVersion"
    )

    price: float | None = None
    formatted_price: str | None = Field(default=None, alias="formattedPrice")
    genres: list[str] = Field(default_factory=list)
    primary_genre_name: str | None = Field(default=None, alias="primaryGenreName")
    minimum_os_version: str | None = Field(default=None, alias="minimumOsVersion")

    description: str | None = None
    release_notes: str | None = Field(default=None, alias="releaseNotes")
    screenshot_urls: list[str] = Field(default_factory=list, alias="screenshotUrls")
    advisories: list[str] = Field(default_factory=list)
    content_advisory_rating: str | None = Field(default=None, alias="contentAdvisoryRating")
    track_view_url: str | None = Field(default=None, alias="trackViewUrl")

    @field_validator("current_version_release_date", "release_date", mode="before")
    @classmethod
    def _coerce_dt(cls, v: Any) -> datetime | None:
        return parse_apple_datetime(v)

    @field_validator(
        "price", "average_user_rating", "average_user_rating_current_version", mode="before"
    )
    @classmethod
    def _coerce_float(cls, v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @field_validator("user_rating_count", "user_rating_count_current_version", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @property
    def store_url(self) -> str:
        return self.track_view_url or f"https://apps.apple.com/us/app/id{self.track_id}"


class SearchResponse(BaseModel):
    result_count: int = Field(alias="resultCount")
    results: list[AppRecord] = Field(default_factory=list)


class ReviewRecord(BaseModel):
    """One review from the customerreviews RSS feed."""

    review_id: str
    track_id: int
    rating: int
    title: str | None = None
    body: str | None = None
    app_version: str | None = None
    author: str | None = None
    updated_at: datetime

    @classmethod
    def from_feed_entry(cls, entry: dict[str, Any], track_id: int) -> ReviewRecord | None:
        """Build from a raw feed entry, or None if the entry isn't a review.

        Page 1's first entry is sometimes app metadata rather than a review; it carries
        no `im:rating`, which is what we key on.
        """
        rating_raw = _unlabel(entry.get("im:rating"))
        review_id = _unlabel(entry.get("id"))
        updated = parse_apple_datetime(_unlabel(entry.get("updated")))
        if rating_raw is None or review_id is None or updated is None:
            return None
        try:
            rating = int(rating_raw)
        except ValueError:
            return None

        author = entry.get("author") or {}
        author_name = _unlabel(author.get("name")) if isinstance(author, dict) else None

        return cls(
            review_id=review_id,
            track_id=track_id,
            rating=rating,
            title=_unlabel(entry.get("title")),
            body=_unlabel(entry.get("content")),
            app_version=_unlabel(entry.get("im:version")),
            author=author_name,
            updated_at=updated,
        )


def entries_from_feed(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract entries, tolerating the single-entry-is-a-dict and no-entry cases."""
    feed = payload.get("feed")
    if not isinstance(feed, dict):
        return []
    entry = feed.get("entry")
    if entry is None:
        return []
    if isinstance(entry, dict):
        return [entry]
    if isinstance(entry, list):
        return [e for e in entry if isinstance(e, dict)]
    return []

"""SQLite access layer.

Single-writer, local, zero-ops. All timestamps in and out are UTC-aware; the DB stores
ISO-8601 strings so the schema ports to Postgres without a conversion pass.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from screener.logging import get_logger
from screener.sources.models import AppRecord, ReviewRecord

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = "1"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Read a stored timestamp back as UTC-aware."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = connect(path)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- migrations

    def migrate(self) -> None:
        """Apply the schema. Idempotent -- every statement is CREATE ... IF NOT EXISTS."""
        self.conn.executescript(SCHEMA_PATH.read_text("utf-8"))
        self.conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        log.info("db.migrated", path=str(self.path), version=SCHEMA_VERSION)

    # ---------------------------------------------------------------------- apps

    def upsert_app(self, app: AppRecord) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO apps (track_id, bundle_id, platform, name, seller_name,
                              primary_genre, genres, release_date, store_url,
                              description, first_seen_at, last_seen_at)
            VALUES (?, ?, 'ios', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (track_id) DO UPDATE SET
                bundle_id     = excluded.bundle_id,
                name          = excluded.name,
                seller_name   = excluded.seller_name,
                primary_genre = excluded.primary_genre,
                genres        = excluded.genres,
                store_url     = excluded.store_url,
                description   = excluded.description,
                last_seen_at  = excluded.last_seen_at
            """,
            (
                app.track_id,
                app.bundle_id,
                app.track_name,
                app.seller_name,
                app.primary_genre_name,
                json.dumps(app.genres),
                _iso(app.release_date),
                app.store_url,
                app.description,
                now,
                now,
            ),
        )

    def get_app(self, track_id: int) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM apps WHERE track_id = ?", (track_id,))
        row: sqlite3.Row | None = cur.fetchone()
        return row

    def all_track_ids(self) -> list[int]:
        cur = self.conn.execute("SELECT track_id FROM apps ORDER BY track_id")
        return [int(r["track_id"]) for r in cur.fetchall()]

    # ----------------------------------------------------------------- snapshots

    def upsert_snapshot(self, app: AppRecord, captured_at: datetime | None = None) -> bool:
        """Record today's snapshot. Returns True if this created a new row.

        Re-running on the same day updates the existing row rather than inserting a
        duplicate that would corrupt velocity math.
        """
        ts = captured_at or utc_now()
        captured_date = ts.astimezone(UTC).date().isoformat()
        cur = self.conn.execute(
            "SELECT 1 FROM app_snapshots WHERE track_id = ? AND captured_date = ?",
            (app.track_id, captured_date),
        )
        existed = cur.fetchone() is not None

        self.conn.execute(
            """
            INSERT INTO app_snapshots (
                track_id, captured_at, captured_date, version,
                current_version_release_date, user_rating_count, average_user_rating,
                user_rating_count_current_version, average_user_rating_current_version,
                price, formatted_price, minimum_os_version, release_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (track_id, captured_date) DO UPDATE SET
                captured_at                         = excluded.captured_at,
                version                             = excluded.version,
                current_version_release_date        = excluded.current_version_release_date,
                user_rating_count                   = excluded.user_rating_count,
                average_user_rating                 = excluded.average_user_rating,
                user_rating_count_current_version   = excluded.user_rating_count_current_version,
                average_user_rating_current_version = excluded.average_user_rating_current_version,
                price                               = excluded.price,
                formatted_price                     = excluded.formatted_price,
                minimum_os_version                  = excluded.minimum_os_version,
                release_notes                       = excluded.release_notes
            """,
            (
                app.track_id,
                _iso(ts),
                captured_date,
                app.version,
                _iso(app.current_version_release_date),
                app.user_rating_count,
                app.average_user_rating,
                app.user_rating_count_current_version,
                app.average_user_rating_current_version,
                app.price,
                app.formatted_price,
                app.minimum_os_version,
                app.release_notes,
            ),
        )
        return not existed

    def latest_snapshot(self, track_id: int) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM app_snapshots WHERE track_id = ? " "ORDER BY captured_date DESC LIMIT 1",
            (track_id,),
        )
        row: sqlite3.Row | None = cur.fetchone()
        return row

    def snapshots(self, track_id: int) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM app_snapshots WHERE track_id = ? ORDER BY captured_date ASC",
            (track_id,),
        )
        return list(cur.fetchall())

    def snapshot_dates(self) -> list[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT captured_date FROM app_snapshots ORDER BY captured_date"
        )
        return [str(r["captured_date"]) for r in cur.fetchall()]

    # ------------------------------------------------------------------- reviews

    def upsert_reviews(self, reviews: Iterable[ReviewRecord]) -> tuple[int, int]:
        """Insert new reviews, update edited ones. Returns (inserted, updated).

        An edited review keeps its ID and changes its body; that's an update, not a new
        row, otherwise velocity double-counts one user's opinion.
        """
        inserted = updated = 0
        now = utc_now_iso()
        for r in reviews:
            cur = self.conn.execute(
                "SELECT body, rating, updated_at FROM reviews WHERE review_id = ?",
                (r.review_id,),
            )
            existing = cur.fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO reviews (review_id, track_id, rating, title, body,
                                         app_version, author, updated_at, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.review_id,
                        r.track_id,
                        r.rating,
                        r.title,
                        r.body,
                        r.app_version,
                        r.author,
                        _iso(r.updated_at),
                        now,
                    ),
                )
                inserted += 1
                continue

            changed = (
                existing["body"] != r.body
                or int(existing["rating"]) != r.rating
                or existing["updated_at"] != _iso(r.updated_at)
            )
            if changed:
                self.conn.execute(
                    """
                    UPDATE reviews SET rating = ?, title = ?, body = ?, app_version = ?,
                                       updated_at = ?, fetched_at = ?, revised_at = ?
                    WHERE review_id = ?
                    """,
                    (
                        r.rating,
                        r.title,
                        r.body,
                        r.app_version,
                        _iso(r.updated_at),
                        now,
                        now,
                        r.review_id,
                    ),
                )
                updated += 1
        return inserted, updated

    def reviews_for(
        self,
        track_id: int,
        *,
        since: datetime | None = None,
        max_rating: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM reviews WHERE track_id = ?"
        params: list[Any] = [track_id]
        if since is not None:
            sql += " AND updated_at >= ?"
            params.append(_iso(since))
        if max_rating is not None:
            sql += " AND rating <= ?"
            params.append(max_rating)
        sql += " ORDER BY updated_at DESC"
        return list(self.conn.execute(sql, params).fetchall())

    def review_count(self, track_id: int) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM reviews WHERE track_id = ?", (track_id,))
        return int(cur.fetchone()["n"])

    # ------------------------------------------------------------------ keywords

    def upsert_keyword(self, term: str, source: str = "seed") -> int:
        self.conn.execute(
            "INSERT INTO keywords (term, source) VALUES (?, ?) " "ON CONFLICT (term) DO NOTHING",
            (term, source),
        )
        cur = self.conn.execute("SELECT id FROM keywords WHERE term = ?", (term,))
        return int(cur.fetchone()["id"])

    def mark_keyword_searched(self, keyword_id: int, yield_count: int) -> None:
        self.conn.execute(
            "UPDATE keywords SET last_searched_at = ?, yield_count = ? WHERE id = ?",
            (utc_now_iso(), yield_count, keyword_id),
        )

    def active_keywords(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM keywords WHERE active = 1 ORDER BY last_searched_at IS NOT NULL, "
                "last_searched_at ASC"
            ).fetchall()
        )

    def keyword_yield(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
                SELECT k.id, k.term, k.source, k.last_searched_at, k.yield_count,
                       COUNT(DISTINCT h.track_id) AS hits,
                       COUNT(DISTINCT CASE WHEN s.passed_filters = 1 THEN h.track_id END)
                           AS qualified
                FROM keywords k
                LEFT JOIN discovery_hits h ON h.keyword_id = k.id
                LEFT JOIN scores s ON s.track_id = h.track_id
                GROUP BY k.id
                ORDER BY qualified DESC, hits DESC
                """).fetchall())

    def record_hits(self, keyword_id: int, ranked: Sequence[tuple[int, int]]) -> None:
        now = utc_now_iso()
        self.conn.executemany(
            "INSERT INTO discovery_hits (keyword_id, track_id, rank, seen_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [(keyword_id, track_id, rank, now) for track_id, rank in ranked],
        )

    def keyword_cohort(
        self,
        track_id: int,
        *,
        top_rank: int | None = None,
        same_genre: bool = False,
    ) -> list[int]:
        """Apps that plausibly compete with this one for the same users.

        Co-discovery alone is far too loose -- a single search returns ~190 loosely
        related apps, which would make every candidate look surrounded by competitors.
        `top_rank` keeps only apps ranking near the top for a shared keyword (the ones
        actually serving that query), and `same_genre` drops the unrelated remainder.
        """
        sql = """
            SELECT DISTINCT h2.track_id
            FROM discovery_hits h1
            JOIN discovery_hits h2 ON h1.keyword_id = h2.keyword_id
            JOIN apps a2 ON a2.track_id = h2.track_id
            WHERE h1.track_id = ? AND h2.track_id != ?
        """
        params: list[Any] = [track_id, track_id]
        if top_rank is not None:
            sql += " AND h2.rank <= ?"
            params.append(top_rank)
        if same_genre:
            sql += """ AND a2.primary_genre IS NOT NULL
                       AND a2.primary_genre = (
                           SELECT primary_genre FROM apps WHERE track_id = ?
                       )"""
            params.append(track_id)
        cur = self.conn.execute(sql, params)
        return [int(r["track_id"]) for r in cur.fetchall()]

    # -------------------------------------------------------------------- scores

    def insert_score(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO scores (track_id, computed_at, rubric_version, demand_persistence,
                                incumbent_weakness, monetization_ceiling, capability_delta,
                                build_cost, distribution_wedge, composite, passed_filters,
                                kill_reason, metrics_json)
            VALUES (:track_id, :computed_at, :rubric_version, :demand_persistence,
                    :incumbent_weakness, :monetization_ceiling, :capability_delta,
                    :build_cost, :distribution_wedge, :composite, :passed_filters,
                    :kill_reason, :metrics_json)
            ON CONFLICT (track_id, rubric_version, computed_at) DO NOTHING
            """,
            row,
        )

    def latest_scores(
        self, rubric_version: str, *, passed_only: bool = False, limit: int | None = None
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT s.*, a.name, a.seller_name, a.primary_genre, a.store_url
            FROM scores s
            JOIN apps a ON a.track_id = s.track_id
            JOIN (
                SELECT track_id, MAX(computed_at) AS latest
                FROM scores WHERE rubric_version = ? GROUP BY track_id
            ) m ON m.track_id = s.track_id AND m.latest = s.computed_at
            WHERE s.rubric_version = ?
        """
        params: list[Any] = [rubric_version, rubric_version]
        if passed_only:
            sql += " AND s.passed_filters = 1"
        sql += " ORDER BY s.composite DESC NULLS LAST"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params).fetchall())

    def latest_score_for(self, track_id: int, rubric_version: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM scores WHERE track_id = ? AND rubric_version = ? "
            "ORDER BY computed_at DESC LIMIT 1",
            (track_id, rubric_version),
        )
        row: sqlite3.Row | None = cur.fetchone()
        return row

    # ------------------------------------------------------------- manual scores

    def upsert_manual_score(
        self,
        track_id: int,
        rubric_version: str,
        values: dict[str, float | None],
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO manual_scores (track_id, rubric_version, scored_at,
                                       monetization_ceiling, capability_delta,
                                       build_cost, distribution_wedge, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (track_id, rubric_version) DO UPDATE SET
                scored_at            = excluded.scored_at,
                monetization_ceiling = COALESCE(excluded.monetization_ceiling,
                                                manual_scores.monetization_ceiling),
                capability_delta     = COALESCE(excluded.capability_delta,
                                                manual_scores.capability_delta),
                build_cost           = COALESCE(excluded.build_cost, manual_scores.build_cost),
                distribution_wedge   = COALESCE(excluded.distribution_wedge,
                                                manual_scores.distribution_wedge),
                notes                = COALESCE(excluded.notes, manual_scores.notes)
            """,
            (
                track_id,
                rubric_version,
                utc_now_iso(),
                values.get("monetization_ceiling"),
                values.get("capability_delta"),
                values.get("build_cost"),
                values.get("distribution_wedge"),
                notes,
            ),
        )

    def manual_score(self, track_id: int, rubric_version: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM manual_scores WHERE track_id = ? AND rubric_version = ?",
            (track_id, rubric_version),
        )
        row: sqlite3.Row | None = cur.fetchone()
        return row

    # ---------------------------------------------------------------- kill flags

    def set_kill_flag(self, track_id: int, flag: str, value: bool, note: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO kill_flags (track_id, flag, value, noted_at, note) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (track_id, flag) DO UPDATE SET value = excluded.value, "
            "noted_at = excluded.noted_at, note = excluded.note",
            (track_id, flag, int(value), utc_now_iso(), note),
        )

    def kill_flags(self, track_id: int) -> dict[str, bool]:
        cur = self.conn.execute(
            "SELECT flag, value FROM kill_flags WHERE track_id = ?", (track_id,)
        )
        return {str(r["flag"]): bool(r["value"]) for r in cur.fetchall()}

    # ------------------------------------------------------------------ clusters

    def replace_clusters(
        self, track_id: int, computed_at: str, clusters: Sequence[dict[str, Any]]
    ) -> None:
        """Clusters are recomputed wholesale per run; drop this run's prior output."""
        self.conn.execute(
            "DELETE FROM complaint_clusters WHERE track_id = ? AND computed_at = ?",
            (track_id, computed_at),
        )
        self.conn.executemany(
            """
            INSERT INTO complaint_clusters (track_id, computed_at, theme, kind, frequency,
                                            severity, example_review_ids, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    track_id,
                    computed_at,
                    c.get("theme"),
                    c.get("kind"),
                    c.get("frequency"),
                    c.get("severity"),
                    json.dumps(c.get("example_review_ids", [])),
                    c.get("summary"),
                )
                for c in clusters
            ],
        )

    def latest_clusters(self, track_id: int) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM complaint_clusters WHERE track_id = ? AND computed_at = "
            "(SELECT MAX(computed_at) FROM complaint_clusters WHERE track_id = ?) "
            "ORDER BY frequency DESC",
            (track_id, track_id),
        )
        return list(cur.fetchall())

    def record_clustering_run(
        self,
        track_id: int,
        computed_at: str,
        model: str,
        review_count: int,
        batch_count: int,
        raw_output: str | None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO clustering_runs (track_id, computed_at, model, review_count,
                                         batch_count, raw_output, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (track_id, computed_at, model, review_count, batch_count, raw_output, error),
        )

    # --------------------------------------------------------------------- stats

    def counts(self) -> dict[str, int]:
        out = {}
        for table in (
            "apps",
            "app_snapshots",
            "reviews",
            "keywords",
            "discovery_hits",
            "scores",
            "manual_scores",
            "complaint_clusters",
        ):
            cur = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            out[table] = int(cur.fetchone()["n"])
        return out

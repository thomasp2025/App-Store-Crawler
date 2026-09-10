-- App Store Opportunity Screener schema.
--
-- Deliberately Postgres-compatible: no SQLite-only types, no AUTOINCREMENT, timestamps
-- stored as ISO-8601 UTC TEXT (Postgres casts these to timestamptz cleanly).
--
-- Snapshot idempotency is enforced here, not in application code: the UNIQUE on
-- (track_id, captured_date) plus ON CONFLICT DO UPDATE means re-running collection on
-- the same day updates the day's row instead of creating a second one that would
-- double-count in velocity math.

CREATE TABLE IF NOT EXISTS apps (
    track_id        INTEGER PRIMARY KEY,
    bundle_id       TEXT,
    platform        TEXT NOT NULL DEFAULT 'ios',
    name            TEXT NOT NULL,
    seller_name     TEXT,
    primary_genre   TEXT,
    genres          TEXT,             -- JSON array
    release_date    TEXT,             -- ISO-8601 UTC
    store_url       TEXT,
    description     TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_snapshots (
    id                                    INTEGER PRIMARY KEY,
    track_id                              INTEGER NOT NULL REFERENCES apps(track_id),
    captured_at                           TEXT NOT NULL,   -- full ISO-8601 UTC timestamp
    captured_date                         TEXT NOT NULL,   -- YYYY-MM-DD, the idempotency key
    version                               TEXT,
    current_version_release_date          TEXT,
    user_rating_count                     INTEGER,
    average_user_rating                   REAL,
    user_rating_count_current_version     INTEGER,
    average_user_rating_current_version   REAL,
    price                                 REAL,
    formatted_price                       TEXT,
    minimum_os_version                    TEXT,
    release_notes                         TEXT,
    UNIQUE (track_id, captured_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_track_date
    ON app_snapshots (track_id, captured_date);

CREATE TABLE IF NOT EXISTS reviews (
    review_id     TEXT PRIMARY KEY,
    track_id      INTEGER NOT NULL REFERENCES apps(track_id),
    rating        INTEGER NOT NULL,
    title         TEXT,
    body          TEXT,
    app_version   TEXT,
    author        TEXT,
    updated_at    TEXT NOT NULL,      -- ISO-8601 UTC
    fetched_at    TEXT NOT NULL,
    -- An edited review keeps its ID; we bump this instead of inserting a new row.
    revised_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_track_updated
    ON reviews (track_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_reviews_track_rating
    ON reviews (track_id, rating);

CREATE TABLE IF NOT EXISTS keywords (
    id                INTEGER PRIMARY KEY,
    term              TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL DEFAULT 'seed',
    last_searched_at  TEXT,
    yield_count       INTEGER NOT NULL DEFAULT 0,
    -- apps from this keyword that later cleared the hard filters; the real yield signal
    qualified_count   INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS discovery_hits (
    keyword_id  INTEGER NOT NULL REFERENCES keywords(id),
    track_id    INTEGER NOT NULL REFERENCES apps(track_id),
    rank        INTEGER NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (keyword_id, track_id, seen_at)
);

CREATE INDEX IF NOT EXISTS idx_hits_track ON discovery_hits (track_id);
CREATE INDEX IF NOT EXISTS idx_hits_keyword ON discovery_hits (keyword_id);

CREATE TABLE IF NOT EXISTS scores (
    id                     INTEGER PRIMARY KEY,
    track_id               INTEGER NOT NULL REFERENCES apps(track_id),
    computed_at            TEXT NOT NULL,
    rubric_version         TEXT NOT NULL,
    demand_persistence     REAL,
    incumbent_weakness     REAL,
    monetization_ceiling   REAL,
    capability_delta       REAL,
    build_cost             REAL,
    distribution_wedge     REAL,
    composite              REAL,
    passed_filters         INTEGER NOT NULL DEFAULT 0,
    kill_reason            TEXT,
    -- snapshot of the metrics the auto-dimensions were derived from, for traceability
    metrics_json           TEXT,
    UNIQUE (track_id, rubric_version, computed_at)
);

CREATE INDEX IF NOT EXISTS idx_scores_track ON scores (track_id, computed_at);

-- Manual scores are kept separately from computed ones so re-running auto-scoring
-- never destroys human judgment.
CREATE TABLE IF NOT EXISTS manual_scores (
    track_id               INTEGER NOT NULL REFERENCES apps(track_id),
    rubric_version         TEXT NOT NULL,
    scored_at              TEXT NOT NULL,
    monetization_ceiling   REAL,
    capability_delta       REAL,
    build_cost             REAL,
    distribution_wedge     REAL,
    notes                  TEXT,
    PRIMARY KEY (track_id, rubric_version)
);

CREATE TABLE IF NOT EXISTS kill_flags (
    track_id    INTEGER NOT NULL REFERENCES apps(track_id),
    flag        TEXT NOT NULL,
    value       INTEGER NOT NULL DEFAULT 0,
    noted_at    TEXT NOT NULL,
    note        TEXT,
    PRIMARY KEY (track_id, flag)
);

CREATE TABLE IF NOT EXISTS complaint_clusters (
    id                  INTEGER PRIMARY KEY,
    track_id            INTEGER NOT NULL REFERENCES apps(track_id),
    computed_at         TEXT NOT NULL,
    theme               TEXT NOT NULL,
    kind                TEXT,          -- 'broken' | 'missing_feature' | 'other'
    frequency           INTEGER,
    severity            REAL,
    example_review_ids  TEXT,          -- JSON array
    summary             TEXT
);

CREATE INDEX IF NOT EXISTS idx_clusters_track ON complaint_clusters (track_id, computed_at);

-- Raw model output kept alongside parsed clusters, for debugging bad extractions.
CREATE TABLE IF NOT EXISTS clustering_runs (
    id              INTEGER PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES apps(track_id),
    computed_at     TEXT NOT NULL,
    model           TEXT NOT NULL,
    review_count    INTEGER NOT NULL,
    batch_count     INTEGER NOT NULL,
    raw_output      TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

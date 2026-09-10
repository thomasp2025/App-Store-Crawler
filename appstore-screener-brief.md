# App Store Opportunity Screener — Project Brief

## Purpose

Build a crawler + scoring pipeline that finds **stale mobile apps in live markets**: apps with persistent user demand whose maintainers have stopped shipping. Output is a ranked candidate list plus, for each candidate, a clustered summary of what current users complain about (which doubles as a v1 feature spec).

This is a personal research tool, not a product. Optimize for iteration speed and data quality over polish.

---

## The core insight that drives the design

Lifetime download counts are the wrong metric — they include long-dead viral spikes. What matters is **current demand velocity** against **incumbent decay**.

Neither is directly available from public APIs. Both must be derived:

- **Demand velocity** ← review timestamps from the recent-reviews feed (reviews/day over the trailing window)
- **Incumbent decay** ← `currentVersionReleaseDate` staleness + the gap between lifetime rating and current-version rating

The single most valuable asset this tool builds is a **longitudinal snapshot table**. Nobody can retroactively query "was this app's review velocity growing 6 months ago." If we snapshot daily starting now, we own that. **Prioritize getting snapshot collection running before building the scoring layer.** Scoring can be backfilled; missed days cannot.

---

## Stack

- **Python 3.11+**, `httpx` (async), `pydantic` for response models
- **SQLite** via `sqlite3` or SQLAlchemy. Single-writer, local, zero-ops. Schema should stay Postgres-compatible so migration is trivial if the dataset outgrows it.
- **Typer** for CLI
- `google-play-scraper` (PyPI) for the Android side — phase 2, do not build this first
- Anthropic SDK for the review-clustering step

No web UI initially. CLI commands that emit CSV/Markdown reports.

---

## Data sources

### 1. iTunes Search API — discovery

```
GET https://itunes.apple.com/search
  ?term={keyword}&country=us&entity=software&limit=200
```

Returns up to 200 apps per keyword. This is the seed discovery mechanism.

### 2. iTunes Lookup API — enrichment

```
GET https://itunes.apple.com/lookup?id={trackId}&country=us
```

Accepts comma-separated IDs (batch them — but verify the practical cap, historically around 100 per call). This is what gets called on every snapshot run.

Fields to persist from both endpoints:

| Field | Use |
|---|---|
| `trackId`, `bundleId`, `trackName`, `sellerName` | identity |
| `currentVersionReleaseDate` | **staleness signal** |
| `releaseDate` | app age |
| `version` | version churn |
| `userRatingCount` | lifetime rating volume |
| `averageUserRating` | lifetime rating |
| `userRatingCountForCurrentVersion` | recent rating volume |
| `averageUserRatingForCurrentVersion` | **decay signal — compare to lifetime** |
| `price`, `formattedPrice` | monetization model hint |
| `genres`, `primaryGenreName` | category |
| `minimumOsVersion` | staleness corroboration |
| `description`, `releaseNotes` | LLM context |
| `screenshotUrls` | manual review |
| `advisories`, `contentAdvisoryRating` | filtering |

### 3. Customer reviews feed — the important one

```
GET https://itunes.apple.com/{cc}/rss/customerreviews/page={n}/id={trackId}/sortby=mostrecent/json
```

Roughly 50 reviews/page, up to 10 pages (~500 most recent reviews). Each entry carries rating, title, body, author, app version, and `updated` timestamp.

**⚠️ Verify this endpoint before building on it.** Apple has deprecated various RSS feeds over the years and the path format has shifted more than once. First task: probe it against 3–5 known app IDs, confirm the response shape, and record the working URL pattern in `docs/endpoints.md`. If it's dead, fall back options are the `rss.applemarketingtools.com` feeds or a headless fetch of the public web listing — but check the simple path first.

---

## Rate limiting and etiquette

These are undocumented public endpoints with no published limits. Community consensus has long been roughly **20 requests/minute** before 403s appear.

Non-negotiable client behavior:

- Global token-bucket limiter, default **15 req/min**, configurable
- Exponential backoff with jitter on 403/429/5xx; treat sustained 403 as a hard stop, not a retry loop
- **On-disk response cache** keyed by URL + date. Lookup responses cached 24h, review pages 24h, search results 7 days. During development you will re-run constantly — never re-hit the network for data already pulled today.
- Descriptive User-Agent
- Single concurrency knob, defaulted low

Design so a full daily snapshot of ~5,000 tracked apps fits comfortably in an overnight run.

---

## Schema

```
apps                    -- immutable identity
  track_id PK, bundle_id, platform, name, seller_name,
  primary_genre, release_date, first_seen_at

app_snapshots           -- one row per app per collection run
  id PK, track_id FK, captured_at,
  version, current_version_release_date,
  user_rating_count, average_user_rating,
  user_rating_count_current_version, average_user_rating_current_version,
  price, formatted_price, minimum_os_version,
  UNIQUE(track_id, captured_at::date)

reviews                 -- deduped by review id
  review_id PK, track_id FK, rating, title, body,
  app_version, author, updated_at, fetched_at

keywords                -- discovery seeds
  id PK, term, source, last_searched_at, yield_count

discovery_hits          -- keyword → app, with rank
  keyword_id FK, track_id FK, rank, seen_at

scores                  -- computed, versioned so rubric changes are traceable
  track_id FK, computed_at, rubric_version,
  demand_persistence, incumbent_weakness, monetization_ceiling,
  capability_delta, build_cost, distribution_wedge,
  composite, passed_filters BOOL, kill_reason TEXT NULL

complaint_clusters      -- LLM output
  id PK, track_id FK, computed_at, theme, frequency,
  severity, example_review_ids JSON
```

---

## Derived metrics

Implement in a dedicated `metrics.py`, pure functions over rows, unit-tested with fixtures.

**`review_velocity(track_id, window_days=90)`**
From the reviews table, count reviews in the window ÷ window. Before longitudinal data exists, approximate from the timestamp span of the most recent N reviews: `N / (newest - oldest).days`. Note this saturates for very high-volume apps (500 reviews may span only days) — flag saturation rather than reporting a wrong number.

**`velocity_trend(track_id)`**
Trailing-90d velocity ÷ preceding-90d velocity. Requires ~6 months of review history, so it will be null early. Build it anyway.

**`staleness_days`**
`now - currentVersionReleaseDate`.

**`rating_decay`**
`average_user_rating - average_user_rating_current_version`. A positive gap means the current build is rated worse than the app's history — the incumbent is actively degrading. **This is the highest-signal single field available and it is free.** Weight it accordingly.

**`estimated_installs`**
`user_rating_count / rating_rate`, where `rating_rate` is a configurable constant. Historically cited in the 1–3% range, likely higher since in-app rating prompts became standard. **Treat this as an order-of-magnitude bucket, never a number.** Store the assumed rate alongside the estimate. Do not let it into the scoring rubric with meaningful weight.

**`revenue_signal`**
Paid app price, or presence of IAP indicators in description text. Weak from public data — flag apps needing manual paywall inspection rather than guessing.

---

## Pipeline stages

### Stage 1 — Discovery
Seed from a hand-written keyword file (`data/seeds.txt`). Expand via: co-occurring terms in candidate app titles/descriptions, related apps from the same sellers, and manual additions. Log yield per keyword so unproductive seeds get pruned.

Deliberately search **narrow long-tail terms**, not category names. Also capture apps ranked 50–250 in search results, not just the top 10 — the top is where the well-funded incumbents live.

### Stage 2 — Hard filters
Auto-reject. Cheap, runs on every snapshot:

- `staleness_days < 450` → reject
- `velocity_trend < 0.8` (where computable) → reject, market is dying
- `average_user_rating > 4.4` → reject, users are satisfied
- `average_user_rating < 2.8` → reject, likely a broken market not a beatable app
- `user_rating_count < 300` → reject, too small to prove demand
- ≥2 competitors in the same keyword cluster updated within 90 days → reject

All thresholds in a single `config.toml`. They *will* be tuned; do not hardcode.

### Stage 3 — Scoring

Weighted composite, each dimension 0–5:

| Dimension | Weight | Computed from |
|---|---|---|
| Demand persistence | 20% | review velocity, velocity trend |
| Incumbent weakness | 15% | rating_decay, staleness, 1–2★ complaint density |
| Monetization ceiling | 25% | price signals, category priors, **manual input** |
| Capability delta | 20% | **manual input** |
| Build cost (inverted) | 10% | **manual input** |
| Distribution wedge | 10% | **manual input** |

Four of six are manual. That's intentional — the crawler's job is to reduce 50,000 apps to a shortlist of ~30 worth human judgment. Support a two-pass flow: auto-score the computable dimensions, emit a review queue, accept manual scores back via CSV import or a `score` CLI command.

Version the rubric (`rubric_version` column) so rescoring under new weights doesn't destroy history.

### Stage 4 — Kill criteria
Boolean flags, any one is an auto-reject regardless of composite. Manual, prompted during review:

- Plausibly a native OS feature within one platform release
- Requires a licensed data feed
- Requires network effects to be useful on day one
- Trade dress / name is inseparable from the incumbent
- Pricing model mismatch — will this user pay for this, at the model being considered?

### Stage 5 — Complaint clustering
For each shortlisted app, pull all 1–2★ reviews from the trailing 12 months, batch them into an Anthropic API call, and extract themes as structured JSON: `{theme, frequency, severity, example_review_ids}`.

Prompt constraints: no invented themes, every theme must map to specific review IDs, distinguish "app is broken" (crash, iOS incompatibility, login failure) from "app lacks feature X" — those imply very different opportunities. Cap batch size and chunk with a merge pass rather than truncating.

Persist raw model output alongside parsed clusters for debugging.

### Stage 6 — Reporting
`report shortlist` → ranked Markdown table with links to App Store listings.
`report app <track_id>` → one-page dossier: metrics, sparkline of velocity, top complaint clusters with example quotes, competitor set.

---

## Build order

1. **HTTP client** — rate limiter, disk cache, retry logic. Everything depends on this; get it right first.
2. **Endpoint verification** — probe Search, Lookup, and reviews feed. Document actual response shapes in `docs/endpoints.md`. Do not trust this brief's URL formats without checking.
3. **Schema + migrations**
4. **Lookup ingestion + snapshot runner** — get this on a daily cron immediately, even against a small seed set. Time series starts accumulating from day one.
5. **Review ingestion**
6. **Metrics module** — pure functions, unit tested
7. **Hard filters + auto-scoring**
8. **Manual scoring CLI**
9. **Complaint clustering**
10. **Reports**
11. *(Later)* Google Play via `google-play-scraper`

---

## Conventions

- `ruff` + `black`, type hints throughout, `mypy` in CI-strict mode
- All network responses validated through pydantic models — Apple's JSON is inconsistent and fields go missing without warning
- Structured logging (`structlog`), one line per network call with cache hit/miss
- Secrets via `.env`, never committed
- `pytest` with recorded fixtures; **no live network calls in tests**
- Every threshold, weight, and constant in `config.toml`

---

## Things to get right

**Snapshot idempotency.** Re-running collection on the same day must not create duplicate snapshot rows or corrupt velocity math.

**Timezone handling.** Apple returns ISO-8601 with offsets. Store UTC, always. Velocity calculations break silently on naive datetimes.

**Review dedup.** The feed returns overlapping pages. Dedup on review ID, and treat an edited review (same ID, changed body) as an update, not a new row.

**Cache invalidation during dev.** A `--no-cache` flag and a `cache clear` command will save hours.

**Don't over-model early.** The schema above is a starting point. If real responses don't match, change the schema rather than contorting the data.

# App Store Opportunity Screener

Finds **stale mobile apps in live markets** — apps with persistent user demand whose
maintainers have stopped shipping. Emits a ranked candidate list and, per candidate, a
clustered summary of what current users complain about (which doubles as a v1 spec).

Personal research tool. Optimised for iteration speed and data quality, not polish.

---

## Read this first: two findings from the live probe

**1. The reviews RSS feed is alive.** The brief flagged it as possibly dead. It isn't —
the URL format in the brief works as written. Pages 1–10 serve ~50 reviews each; page 11
returns HTTP 400. No fallback to `rss.applemarketingtools.com` needed.

**2. `rating_decay` is dead, and that mattered.** The brief calls it "the highest-signal
single field available and it is free." Apple now returns
`averageUserRatingForCurrentVersion` exactly equal to `averageUserRating` — for **555 of
555** apps sampled, with the rating counts identical too. The store no longer segments
ratings by version.

A *constant* signal is worse than a missing one: a hard `0.0` scored as 1.0/5 — real
evidence of a healthy incumbent — for every app on the store, at the heaviest sub-weight
in the rubric. So `metrics.rating_decay` now detects the degenerate case and returns
`None`, and `metrics.review_rating_decay` reconstructs the signal from the reviews feed
(lifetime rating minus the mean star rating of recent reviews). Full write-up and the
measured table: [`docs/endpoints.md`](docs/endpoints.md).

This is the strongest argument for the brief's own instruction to prioritise snapshots:
the real fix is longitudinal. We record `average_user_rating` daily, and once enough days
accumulate its drift measures incumbent decay directly, with no proxy at all.

---

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # ANTHROPIC_API_KEY needed only for `cluster`
.venv/bin/screener init
```

Verify Apple's endpoints still behave before trusting a crawl:

```bash
screener verify-endpoints
```

## Start collecting today

The snapshot table is the one asset that cannot be backfilled. Nobody can retroactively
query "was this app's review velocity growing six months ago." Get this on a cron now,
even against a small seed set — scoring can always be recomputed later.

```cron
17 3 * * *  cd /path/to/app_store_crawler && .venv/bin/screener run-daily --json-logs >> logs/daily.log 2>&1
```

`run-daily` is discover → snapshot → reviews → score. It is idempotent: re-running on the
same day updates that day's rows rather than duplicating them, so a retry after a failure
is always safe.

## The UI

```bash
pip install -e ".[ui]"
screener serve            # → http://127.0.0.1:8765
```

Localhost only, by design — it reads and writes the real `config.toml` and the real
database, so it is not something to put on a network.

- **Opportunity map** — staleness × rating, dot size = rating volume. The shaded box is
  the live filter. Drag any threshold and the box moves, the scatter repaints and a
  banner tells you how many apps the new setting would admit *before* you commit it.
  Click any dot for the full dossier.
- **Sort & filter** — every column sorts; free-text search across name and seller, plus
  genre and pass/reject filters.
- **Settings** — thresholds, rubric weights and the seed keyword list, written back to
  `config.toml` through `tomlkit` so the comments explaining each threshold survive.
  An edit that would produce an invalid config (weights not summing to 1.0) is rejected
  and rolled back rather than leaving the crawler unable to start.
- **Run stages** — search, snapshot and review ingestion can be kicked off from the
  sidebar. One at a time: the crawler is globally rate-limited and SQLite is a single
  writer, so concurrent runs would fight over both.
- **Manual scoring** — score the four human dimensions with sliders in the detail drawer,
  as an alternative to the CSV round-trip.

Two honesty details worth knowing. The scatter colours by the **server's** verdict until
you actually move a slider — the browser can't evaluate the competitor rule, so showing
a preview as fact would overstate the funnel. Only while dragging does it switch to the
client-side approximation, and the legend changes to say so. Chart colours are checked
with the palette validator rather than by eye: the blue/gray pair clears CVD separation,
normal-vision separation and 3:1 surface contrast in both light and dark.

The UI is a deliberate departure from the brief's "no web UI initially" — added on
request after the CLI was complete.

## Commands

| Command | Does |
|---|---|
| `screener init` | Create the DB, load seed keywords. Safe to re-run. |
| `screener verify-endpoints` | Probe Search, Lookup and reviews; report what works. |
| `screener discover [-t TERM]` | Stage 1 — search seeds, record every hit with its rank. |
| `screener snapshot` | Capture today's row for every tracked app. **Cron this.** |
| `screener fetch-reviews` | Pull the ~500 most recent reviews per app, deduped. |
| `screener score` | Stages 2–4 — hard filters plus the computable dimensions. |
| `screener queue --out review.csv` | Emit the manual review queue. |
| `screener import-scores review.csv` | Merge human scores and kill flags back in. |
| `screener set-score ID --capability-delta 4` | Score one app without a CSV round-trip. |
| `screener cluster [IDs]` | Stage 5 — cluster 1–2★ reviews into themes. |
| `screener report shortlist` | Ranked Markdown table of candidates. |
| `screener report app ID` | One-page dossier. |
| `screener report keywords` | Yield per seed, so dead terms get pruned. |
| `screener status` | Row counts and collection coverage. |
| `screener cache clear [--kind reviews]` | Wipe cached responses. |
| `screener serve` | Local web UI for tuning filters and browsing results. |

Every command takes `--config` and `--no-cache`.

## The two-pass scoring flow

Four of the six rubric dimensions need human judgment. That's deliberate — the crawler's
job is to cut 50,000 apps down to ~30 worth an hour of your attention.

```bash
screener score                          # auto-scores demand + incumbent weakness
screener queue --out review.csv         # apps that passed, awaiting judgment
#   fill monetization_ceiling, capability_delta, build_cost, distribution_wedge (0–5)
#   set any kill flag column to 1 to auto-reject
screener import-scores review.csv       # merges and rescores
```

Composites are renormalised over whichever dimensions have values, so a provisional row
(auto dimensions only) sits on the same 0–5 scale as a fully scored one and the two stay
comparable in a ranking. Manual scores live in their own table, so re-running auto-scoring
never destroys human judgment. Changing weights means bumping `rubric_version` in
`config.toml`, which preserves the old scores rather than overwriting them.

## Rate limiting

Undocumented public endpoints, no published limits, community consensus ~20 req/min
before 403s. Defaults here: **15 req/min**, concurrency 2, exponential backoff with
jitter, and **sustained 403 is a hard stop** — three in a row aborts the run rather than
retrying into a longer ban.

Responses are cached on disk keyed by URL (lookups and review pages 24h, searches 7 days),
so re-running during development costs nothing. `--no-cache` and `screener cache clear`
exist because you will need them.

A full daily snapshot of ~5,000 apps is ~50 lookup calls at 100 IDs each — a few minutes.
Review ingestion dominates any overnight run at up to 10 pages per app.

## Layout

```
src/screener/
  config.py         every threshold and weight, loaded and validated from config.toml
  metrics.py        pure functions — velocity, decay, staleness, installs. Unit tested.
  http/             token bucket, disk cache, retry/hard-stop client
  sources/          pydantic models + the three verified iTunes endpoints
  db/               schema.sql (Postgres-compatible) + access layer
  pipeline/         discovery → snapshot → reviews → filters → scoring → clustering
  reports.py        Markdown/CSV output
config.toml         all tunables. Nothing is hardcoded in Python.
docs/endpoints.md   what the endpoints actually return, and the rating_decay finding
data/seeds.txt      discovery seeds — narrow long-tail terms, not category names
```

## Things that are handled

- **Snapshot idempotency** — enforced in the schema via `UNIQUE(track_id, captured_date)`
  plus `ON CONFLICT DO UPDATE`, not in application code.
- **Timezones** — Apple returns offsets like `-07:00`. Everything is normalised to
  UTC-aware at the parse boundary; naive datetimes break velocity math silently.
- **Review dedup** — deduped across pages within a crawl and again at the DB layer. An
  edited review (same ID, changed body) updates its row rather than inserting a second.
- **Velocity saturation** — the feed caps at ~500 reviews, so a busy app's 500 most recent
  may span three days. That's flagged as a lower bound rather than reported as a rate.
- **Missing data never rejects.** Early on, `velocity_trend` is null for everything;
  rejecting on absent inputs would empty the funnel for reasons unrelated to the apps.
  Filters that lack data report as `skipped`, not `reject`.
- **No invented complaint themes** — any review ID the model returns that wasn't in its
  input is dropped, and a theme left with no evidence is discarded entirely.

## Tests

```bash
.venv/bin/pytest          # 142 tests, no network — all fixtures are recorded
.venv/bin/ruff check src tests && .venv/bin/black --check src tests
.venv/bin/mypy src        # strict
```

Fixtures in `tests/fixtures/` are real captured responses. `tests/test_reports.py` runs
the whole pipeline end-to-end against them, including a re-run to prove idempotency.

## Known gaps

- **`velocity_trend` is null until ~6 months of review history exists.** Built anyway, per
  the brief. Until then the hard filter on it skips rather than rejects.
- **Competitor detection is a proxy.** "Same market" is inferred from co-discovery by a
  shared keyword, narrowed to apps ranking in the top 50 for that term *and* sharing the
  candidate's genre. Without that narrowing the rule rejected 100% of apps — a single
  search returns ~190 loosely related results, so every candidate looked surrounded.
  Both bounds are in `config.toml`.
- **`estimated_installs` is an order-of-magnitude bucket**, never a number, and carries no
  weight in the rubric. The assumed rating rate is stored alongside every estimate.
- **Revenue signal is weak.** Public data can't confirm a paywall, so apps are flagged for
  manual inspection rather than guessed at.
- **Google Play is not built** (phase 2, per the brief). The schema carries a `platform`
  column and `google-play-scraper` is behind the `play` extra.

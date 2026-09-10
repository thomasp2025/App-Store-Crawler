# Endpoint verification

Probed live on **2026-09-10** from a US IP with a descriptive User-Agent.
Re-run `screener verify-endpoints` to refresh this; Apple has broken these before.

## 1. Search — discovery

```
GET https://itunes.apple.com/search?term={term}&country=us&entity=software&limit=200
```

- **Status: WORKING.** `200`, `content-type: text/javascript; charset=utf-8` (parse as JSON anyway).
- Shape: `{"resultCount": int, "results": [ {...44 keys...} ]}`
- `limit=200` is the hard cap. `resultCount` is often **less** than the limit even for broad
  terms (`sleep tracker` returned 190) — the cap is a ceiling, not a guarantee.
- All fields the brief asks for are present on search results directly, including
  `currentVersionReleaseDate`, `userRatingCountForCurrentVersion`,
  `averageUserRatingForCurrentVersion`, `minimumOsVersion`, `advisories`.
  Search results are rich enough to snapshot from without a follow-up lookup, but we
  still re-lookup on snapshot runs because search ranking drifts and lookup is by ID.

## 2. Lookup — enrichment

```
GET https://itunes.apple.com/lookup?id={comma,separated,ids}&country=us
```

- **Status: WORKING.**
- **Batch cap: at least 200 IDs per call** — verified `n=5, 50, 100, 150, 200` all returned
  `resultCount == n`. The brief guessed ~100; the real cap is higher. We default to
  `discovery.lookup_batch_size = 100` anyway to stay clear of a URL-length limit and
  because a failed batch of 100 is cheaper to retry than one of 200.
- Unknown/pulled IDs are silently **omitted** from `results`, not returned as errors.
  Callers must diff requested IDs against returned IDs to detect delisted apps.

## 3. Customer reviews RSS — the important one

```
GET https://itunes.apple.com/{cc}/rss/customerreviews/page={n}/id={trackId}/sortby=mostrecent/json
```

- **Status: WORKING.** The brief's URL format is correct as written — no fallback to
  `rss.applemarketingtools.com` or headless scraping needed.
- ~50 entries per page. **Pages 1–10 serve; page 11+ returns HTTP 400** (a hard error, not
  an empty feed). So ~500 most recent reviews is the true ceiling. Treat `400` on a page
  request as end-of-feed, not as a failure to retry.
- Pages 1 and 2 showed **zero ID overlap** in probing, but pages shift between requests as
  new reviews land, so dedup on review ID is still mandatory.

### Entry shape — everything is nested under `label`

```json
{
  "author":  {"uri": {"label": "..."}, "name": {"label": "NeedMoSleep"}},
  "updated": {"label": "2026-09-09T00:24:02-07:00"},
  "im:rating":  {"label": "3"},
  "im:version": {"label": "6.26.35"},
  "id":      {"label": "14528196076"},
  "title":   {"label": "Watch Ultra"},
  "content": {"label": "full review body ..."}
}
```

Gotchas encoded in `sources/itunes.py`:

- **Timestamps carry a non-UTC offset** (`-07:00` above). Naive parsing silently corrupts
  every velocity calculation. Always `parse_apple_datetime` → UTC-aware.
- Ratings and versions arrive as **strings**, not numbers.
- When a feed has exactly one entry, `feed.entry` is a **dict, not a list**.
- A feed with no reviews omits `entry` entirely.
- Page 1's first entry is sometimes app metadata rather than a review (it carries
  `im:name`/`im:price` and no `im:rating`); entries without a rating are dropped.

## Rate limiting

No published limits. Community consensus ~20 req/min before `403`s. We default to 15/min
with a global token bucket, and treat 3 consecutive `403`s as a hard stop rather than
retrying into a ban.

---

## Finding: current-version rating fields are dead (2026-09-10)

The brief calls `rating_decay` — lifetime rating minus current-version rating — "the
highest-signal single field available and it is free," and weights it accordingly.
**It is no longer available.**

Measured across all 555 apps from the first live discovery run:

| Check | Count |
|---|---|
| snapshots total | 555 |
| `averageUserRatingForCurrentVersion` null | 0 |
| **identical to `averageUserRating`** | **555 (100%)** |
| showing any decay (> 0.01) | **0** |

`userRatingCountForCurrentVersion` likewise equals `userRatingCount` exactly, for every
app. Apple is echoing the lifetime figures into the current-version fields rather than
segmenting ratings by version — consistent with the App Store's move to carry ratings
across releases by default.

### Why this mattered more than it looks

A missing signal is cheap; a *constant* signal is expensive. `rating_decay` returning a
hard `0.0` for every app on the store scored as **1.0 out of 5 — real evidence of a
healthy incumbent — for all of them**, at the single heaviest sub-weight in the rubric.
That doesn't just waste the dimension, it compresses the spread of everything measured
alongside it.

### What we do instead

1. `metrics.rating_decay` detects the degenerate case (both averages *and* both counts
   identical) and returns `None` — "not measurable" — so the scorer's sub-weight
   renormalisation drops it cleanly instead of scoring it as evidence. The field is
   still computed, so if Apple ever restores segmentation it starts working again with
   no code change.
2. `metrics.review_rating_decay` reconstructs the signal from a source that still
   works: the lifetime rating minus the mean star rating of reviews in the trailing
   window. The reviews feed still carries a per-review rating and timestamp, so the
   "is this app rated worse now than historically" comparison survives.

Review writers skew more polarised than star-only raters, so the review-derived figure
runs positive even for healthy apps. It gets its own wider breakpoints
(`review_decay_breakpoints`) and must never be scored against the API field's.

**The real replacement is longitudinal.** We snapshot `average_user_rating` daily; once
enough days accumulate, its drift measures incumbent decay directly, with no proxy. This
is the clearest argument in the whole project for the brief's instruction to get
snapshot collection running before anything else.

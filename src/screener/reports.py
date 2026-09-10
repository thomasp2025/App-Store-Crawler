"""Stage 6 -- Reporting. Markdown and CSV out; no web UI."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any

from screener.config import Config, ScoringConfig
from screener.db import Database, parse_iso
from screener.pipeline.context import build_metrics
from screener.pipeline.scoring import score_app

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float | None]) -> str:
    """Render a series as block characters. Gaps render as a space."""
    present = [v for v in values if v is not None]
    if not present:
        return ""
    lo, hi = min(present), max(present)
    span = hi - lo
    out = []
    for v in values:
        if v is None:
            out.append(" ")
            continue
        idx = 0 if span == 0 else int((v - lo) / span * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def _fmt(value: Any, spec: str = "", dash: str = "—") -> str:
    if value is None or value == "":
        return dash
    if spec:
        try:
            return format(value, spec)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def shortlist_markdown(db: Database, config: Config, limit: int | None = None) -> str:
    """Ranked Markdown table of apps that cleared the filters."""
    limit = limit or config.report.shortlist_limit
    rows = db.latest_scores(config.scoring.rubric_version, passed_only=True, limit=limit)

    header = (
        f"# Shortlist — rubric `{config.scoring.rubric_version}`\n\n"
        f"{len(rows)} apps cleared the hard filters.\n\n"
    )
    if not rows:
        return header + (
            "_Nothing has cleared the filters yet. Early on this is expected: "
            "`velocity_trend` needs ~6 months of review history, and most filters "
            "skip rather than reject when their input is missing._\n"
        )

    lines = [
        header,
        "| # | App | Seller | Genre | Stale (d) | Rating | Decay★ | Reviews/d | "
        "Demand | Weakness | Composite | Status |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, start=1):
        m = json.loads(r["metrics_json"] or "{}")
        needs_manual = any(r[d] is None for d in ScoringConfig.MANUAL_DIMENSIONS)
        status = "⚠️ needs manual scoring" if needs_manual else "scored"
        vel = m.get("velocity")
        vel_txt = _fmt(vel, ".2f")
        if m.get("velocity_saturated"):
            vel_txt += "+"  # lower bound, feed saturated
        lines.append(
            f"| {i} | [{r['name']}]({r['store_url'] or ''}) | {r['seller_name'] or '—'} "
            f"| {r['primary_genre'] or '—'} | {_fmt(m.get('staleness_days'))} "
            f"| {_fmt(m.get('average_user_rating'), '.2f')} "
            f"| {_fmt(m.get('review_rating_decay'), '+.2f')} | {vel_txt} "
            f"| {_fmt(r['demand_persistence'], '.1f')} "
            f"| {_fmt(r['incumbent_weakness'], '.1f')} "
            f"| **{_fmt(r['composite'], '.2f')}** | {status} |"
        )

    lines.append(
        "\n_`Decay★` is the lifetime rating minus the mean star rating of recent "
        "reviews; positive means the app is rated worse now than historically. It "
        "replaces the API's current-version rating, which Apple no longer reports "
        "(docs/endpoints.md). Review writers skew polarised, so read it as a relative "
        "ranking, not an absolute._"
        "\n_A `+` on reviews/day means the feed is saturated and the figure is a lower "
        "bound._"
        "\n_Composite is renormalised over the dimensions that have values, so rows "
        "still awaiting manual scoring remain comparable._\n"
    )
    return "\n".join(lines)


def app_dossier(db: Database, track_id: int, config: Config) -> str:
    """One-page dossier for a single app."""
    m = build_metrics(db, track_id, config)
    if m is None:
        return f"No app with track_id {track_id}.\n"
    result, _, verdict = score_app(db, track_id, config)

    out = [f"# {m.name}", ""]
    out.append(
        f"**{m.seller_name or 'unknown seller'}** · {m.primary_genre or 'uncategorised'} "
        f"· `{track_id}`"
    )
    if m.store_url:
        out.append(f"\n[App Store listing]({m.store_url})")

    out.append("\n## Metrics\n")
    out.append("| Metric | Value | Note |")
    out.append("|---|---|---|")
    out.append(f"| Staleness | {_fmt(m.staleness_days)} days | since last shipped version |")
    out.append(f"| Current version | {_fmt(m.version)} | |")
    out.append(
        f"| Lifetime rating | {_fmt(m.average_user_rating, '.2f')} "
        f"| {_fmt(m.user_rating_count)} ratings |"
    )
    out.append(
        f"| Current-version rating | " f"{_fmt(m.average_user_rating_current_version, '.2f')} | |"
    )
    decay_note = (
        "Apple no longer reports a current-version rating — see docs/endpoints.md"
        if m.rating_decay is None
        else ("current build rated worse than history" if m.rating_decay > 0 else "no decay")
    )
    out.append(f"| Rating decay (API) | {_fmt(m.rating_decay, '+.2f')} | {decay_note} |")
    out.append(
        f"| **Rating decay (reviews)** | {_fmt(m.review_rating_decay, '+.2f')} "
        f"| lifetime rating vs mean of recent review stars |"
    )

    if m.velocity:
        note = f"method: {m.velocity.method}, n={m.velocity.sample_size}"
        if m.velocity.saturated:
            note += " — **saturated, lower bound only**"
        out.append(f"| Review velocity | {_fmt(m.velocity.value, '.2f')}/day | {note} |")
    out.append(
        f"| Velocity trend | {_fmt(m.velocity_trend, '.2f')} "
        f"| trailing vs preceding {config.metrics.velocity_window_days}d |"
    )
    out.append(f"| 1–2★ density | {_fmt(m.complaint_density, '.0%')} | of recent reviews |")
    if m.installs:
        out.append(
            f"| Installs (est.) | {m.installs.bucket} | order of magnitude only, "
            f"assumes {m.installs.assumed_rating_rate:.0%} rating rate |"
        )
    if m.revenue:
        flag = " — **inspect paywall manually**" if m.revenue.needs_manual_check else ""
        out.append(
            f"| Monetization | {m.revenue.model} ({_fmt(m.formatted_price)}) "
            f"| {', '.join(m.revenue.iap_hints[:3]) or 'no IAP language'}{flag} |"
        )
    out.append(
        f"| Rating growth | {_fmt(m.rating_count_growth, '.2f')}/day "
        f"| from {m.snapshot_days} snapshots |"
    )
    out.append(f"| Reviews stored | {m.review_count} | |")

    # Sparkline of rating-count growth across snapshots.
    snaps = db.snapshots(track_id)
    series = [s["user_rating_count"] for s in snaps]
    if len([v for v in series if v is not None]) >= 2:
        out.append(f"\n**Rating volume** `{sparkline(series)}` " f"({len(snaps)} snapshots)")
    else:
        out.append(
            f"\n_Only {len(snaps)} snapshot(s) so far — the velocity sparkline needs "
            f"several days of collection before it says anything._"
        )

    out.append("\n## Screening\n")
    if verdict is not None:
        out.append(f"- **Hard filters:** {'PASS' if verdict.passed else 'REJECT'}")
        for reason in verdict.reasons:
            out.append(f"  - reject: {reason}")
        if verdict.skipped:
            out.append(f"  - skipped (no data): {', '.join(verdict.skipped)}")
    if result.kill_reason and result.kill_reason.startswith("kill:"):
        out.append(f"- **Kill flag:** {result.kill_reason}")

    out.append("\n### Scores\n")
    out.append("| Dimension | Weight | Score |")
    out.append("|---|---|---|")
    for dim, weight in config.scoring.weights.items():
        value = result.dimensions.get(dim)
        marker = " _(manual)_" if dim in ScoringConfig.MANUAL_DIMENSIONS else ""
        out.append(
            f"| {dim.replace('_', ' ').title()}{marker} | {weight:.0%} " f"| {_fmt(value, '.1f')} |"
        )
    out.append(f"| **Composite** | | **{_fmt(result.composite, '.2f')}** |")
    if result.manual_missing:
        out.append(
            f"\n_Provisional — awaiting manual scores for " f"{', '.join(result.manual_missing)}._"
        )

    clusters = db.latest_clusters(track_id)
    out.append("\n## Complaint clusters\n")
    if not clusters:
        out.append("_Not clustered yet. Run `screener cluster " f"{track_id}`._")
    else:
        for c in clusters:
            kind = {"broken": "🔧 broken", "missing_feature": "➕ missing feature"}.get(
                str(c["kind"]), str(c["kind"] or "other")
            )
            out.append(f"### {c['theme']}  ·  {kind}")
            out.append(f"*{c['frequency']} reviews · severity {_fmt(c['severity'], '.1f')}*\n")
            if c["summary"]:
                out.append(f"{c['summary']}\n")
            ids = json.loads(c["example_review_ids"] or "[]")
            for rid in ids[:3]:
                row = db.conn.execute(
                    "SELECT rating, title, body, updated_at FROM reviews WHERE review_id = ?",
                    (rid,),
                ).fetchone()
                if row is None:
                    continue
                body = (row["body"] or "").strip().replace("\n", " ")
                if len(body) > 240:
                    body = body[:240].rstrip() + "…"
                date = str(parse_iso(row["updated_at"]))[:10]
                out.append(f"> {'★' * int(row['rating'])} **{row['title'] or ''}** ({date})  ")
                out.append(f"> {body}\n")

    cohort = db.keyword_cohort(
        track_id,
        top_rank=config.filters.competitor_cohort_top_rank,
        same_genre=config.filters.competitor_require_same_genre,
    )
    if cohort:
        out.append(
            f"\n## Competitor set\n\n{len(cohort)} apps co-discovered by the same " f"keywords.\n"
        )
        out.append("| App | Last shipped | Rating |")
        out.append("|---|---|---|")
        for other in cohort[:12]:
            row = db.get_app(other)
            snap = db.latest_snapshot(other)
            if row is None or snap is None:
                continue
            released = parse_iso(snap["current_version_release_date"])
            out.append(
                f"| {row['name']} | {str(released)[:10] if released else '—'} "
                f"| {_fmt(snap['average_user_rating'], '.2f')} |"
            )

    if m.notes:
        out.append("\n## Caveats\n")
        out.extend(f"- {n}" for n in m.notes)

    return "\n".join(out) + "\n"


def rows_to_csv(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def keyword_yield_markdown(db: Database) -> str:
    rows = db.keyword_yield()
    out = [
        "# Keyword yield\n",
        "Unproductive seeds should be pruned from `data/seeds.txt`.\n",
        "| Term | Source | Apps found | Cleared filters | Last searched |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['term']} | {r['source']} | {r['hits']} | {r['qualified']} "
            f"| {str(r['last_searched_at'] or '—')[:10]} |"
        )
    return "\n".join(out) + "\n"

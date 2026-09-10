"""Stage 3 -- Scoring, and Stage 4 -- kill criteria.

Two of six dimensions are computable from crawled data; four need human judgment. That
split is the point of the tool: reduce 50,000 apps to ~30 worth a human hour.

Flow is two-pass. `run_auto_scoring` scores what it can and emits a review queue;
manual scores arrive later by CSV or the `score` command and are merged on top without
destroying the computed half.

Rubric changes get a new `rubric_version` so rescoring never overwrites history.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from screener.config import Config, ScoringConfig
from screener.db import Database, utc_now
from screener.logging import get_logger
from screener.pipeline.context import AppMetrics, build_metrics
from screener.pipeline.filters import FilterVerdict, apply_hard_filters

log = get_logger(__name__)

MAX_DIMENSION_SCORE = 5.0


def bucket_score(value: float | None, breakpoints: Sequence[float]) -> float | None:
    """Map a value onto 0-5 using 5 ascending upper-edge breakpoints."""
    if value is None:
        return None
    score = 0.0
    for i, edge in enumerate(breakpoints, start=1):
        if value >= edge:
            score = float(i)
    return min(score, MAX_DIMENSION_SCORE)


@dataclass
class ScoreResult:
    track_id: int
    dimensions: dict[str, float | None] = field(default_factory=dict)
    composite: float | None = None
    passed_filters: bool = False
    kill_reason: str | None = None
    manual_missing: list[str] = field(default_factory=list)

    @property
    def is_provisional(self) -> bool:
        """True while manual dimensions are still unscored."""
        return bool(self.manual_missing)


def score_demand_persistence(m: AppMetrics, cfg: ScoringConfig) -> float | None:
    """Review velocity, nudged by trend."""
    if m.velocity is None or m.velocity.value is None:
        return None
    base = bucket_score(m.velocity.value, cfg.velocity_breakpoints)
    if base is None:
        return None
    if m.velocity_trend is not None and m.velocity_trend >= cfg.trend_bonus_at:
        base = min(MAX_DIMENSION_SCORE, base + cfg.trend_bonus)
    return base


def score_incumbent_weakness(m: AppMetrics, cfg: ScoringConfig) -> float | None:
    """rating_decay, its review-derived substitute, staleness, and 1-2* complaint density.

    Sub-weights are renormalised over whichever components have data, so an app with no
    reviews yet isn't penalised into a low score for missing complaint density -- and,
    more importantly, so the near-universally-unavailable API decay field drops out
    cleanly instead of scoring every app identically. See `metrics.rating_decay`.
    """
    parts: list[tuple[float, float]] = []  # (score, weight)

    decay = bucket_score(m.rating_decay, cfg.decay_breakpoints)
    if decay is not None:
        parts.append((decay, cfg.decay_weight))

    review_decay = bucket_score(m.review_rating_decay, cfg.review_decay_breakpoints)
    if review_decay is not None:
        parts.append((review_decay, cfg.review_decay_weight))

    stale = bucket_score(
        float(m.staleness_days) if m.staleness_days is not None else None, cfg.staleness_breakpoints
    )
    if stale is not None:
        parts.append((stale, cfg.staleness_weight))

    density = bucket_score(m.complaint_density, cfg.complaint_density_breakpoints)
    if density is not None:
        parts.append((density, cfg.complaint_density_weight))

    if not parts:
        return None
    total_weight = sum(w for _, w in parts)
    if total_weight <= 0:
        return None
    return sum(s * w for s, w in parts) / total_weight


def composite_score(dimensions: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """Weighted mean over the dimensions that have values.

    Renormalised over present weights, so a provisional composite (auto dimensions only)
    is on the same 0-5 scale as a complete one and the two are comparable in a ranking.
    """
    present: list[tuple[float, float]] = [
        (float(value), weights[key])
        for key in weights
        if (value := dimensions.get(key)) is not None
    ]
    if not present:
        return None
    total_weight = sum(weight for _, weight in present)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in present) / total_weight


def check_kill_flags(db: Database, track_id: int, config: Config) -> str | None:
    """Stage 4. Any true flag is an auto-reject regardless of composite."""
    flags = db.kill_flags(track_id)
    tripped = [f for f in config.kill_criteria if flags.get(f)]
    return "kill:" + ",".join(tripped) if tripped else None


def score_app(
    db: Database, track_id: int, config: Config, *, now: datetime | None = None
) -> tuple[ScoreResult, AppMetrics | None, FilterVerdict | None]:
    m = build_metrics(db, track_id, config, now=now)
    if m is None:
        return ScoreResult(track_id=track_id, kill_reason="unknown app"), None, None

    verdict = apply_hard_filters(m, config, db=db)
    scfg = config.scoring

    dimensions: dict[str, float | None] = {
        "demand_persistence": score_demand_persistence(m, scfg),
        "incumbent_weakness": score_incumbent_weakness(m, scfg),
    }

    manual = db.manual_score(track_id, scfg.rubric_version)
    missing: list[str] = []
    for dim in ScoringConfig.MANUAL_DIMENSIONS:
        value = manual[dim] if manual is not None else None
        dimensions[dim] = value
        if value is None:
            missing.append(dim)

    kill = check_kill_flags(db, track_id, config)
    passed = verdict.passed and kill is None
    kill_reason = kill or verdict.kill_reason

    return (
        ScoreResult(
            track_id=track_id,
            dimensions=dimensions,
            composite=composite_score(dimensions, scfg.weights),
            passed_filters=passed,
            kill_reason=kill_reason,
            manual_missing=missing,
        ),
        m,
        verdict,
    )


@dataclass
class ScoringRunResult:
    scored: int = 0
    passed: int = 0
    rejected: int = 0
    needs_manual: int = 0


def run_auto_scoring(
    db: Database,
    config: Config,
    *,
    track_ids: list[int] | None = None,
    now: datetime | None = None,
) -> ScoringRunResult:
    ids = track_ids if track_ids is not None else db.all_track_ids()
    computed_at = (now or utc_now()).isoformat()
    out = ScoringRunResult()

    for track_id in ids:
        result, m, _ = score_app(db, track_id, config, now=now)
        if m is None:
            continue
        db.insert_score(
            {
                "track_id": track_id,
                "computed_at": computed_at,
                "rubric_version": config.scoring.rubric_version,
                **{k: result.dimensions.get(k) for k in config.scoring.weights},
                "composite": result.composite,
                "passed_filters": int(result.passed_filters),
                "kill_reason": result.kill_reason,
                "metrics_json": json.dumps(m.to_json()),
            }
        )
        out.scored += 1
        if result.passed_filters:
            out.passed += 1
            if result.is_provisional:
                out.needs_manual += 1
        else:
            out.rejected += 1

    log.info(
        "scoring.done",
        scored=out.scored,
        passed=out.passed,
        rejected=out.rejected,
        needs_manual=out.needs_manual,
    )
    return out


def import_manual_scores(db: Database, rows: Sequence[dict[str, str]], config: Config) -> int:
    """Merge manual dimension scores from CSV rows. Blank cells leave values untouched."""
    count = 0
    for row in rows:
        raw_id = (row.get("track_id") or "").strip()
        if not raw_id:
            continue
        try:
            track_id = int(raw_id)
        except ValueError:
            log.warning("manual_import.bad_track_id", value=raw_id)
            continue

        values: dict[str, float | None] = {}
        for dim in ScoringConfig.MANUAL_DIMENSIONS:
            cell = (row.get(dim) or "").strip()
            if not cell:
                values[dim] = None
                continue
            try:
                value = float(cell)
            except ValueError:
                log.warning("manual_import.bad_value", track_id=track_id, dim=dim, value=cell)
                values[dim] = None
                continue
            if not 0.0 <= value <= MAX_DIMENSION_SCORE:
                log.warning("manual_import.out_of_range", track_id=track_id, dim=dim, value=value)
                values[dim] = None
                continue
            values[dim] = value

        db.upsert_manual_score(
            track_id,
            config.scoring.rubric_version,
            values,
            notes=(row.get("notes") or "").strip() or None,
        )

        # Kill flags can ride along in the same CSV.
        for flag in config.kill_criteria:
            cell = (row.get(flag) or "").strip().lower()
            if cell in ("1", "true", "yes", "y"):
                db.set_kill_flag(track_id, flag, True)
            elif cell in ("0", "false", "no", "n"):
                db.set_kill_flag(track_id, flag, False)
        count += 1
    return count


def review_queue_rows(
    db: Database, config: Config, limit: int | None = None
) -> list[dict[str, object]]:
    """Apps that cleared the hard filters and still need human dimensions."""
    rows: list[dict[str, object]] = []
    for track_id in db.all_track_ids():
        result, m, _ = score_app(db, track_id, config)
        if m is None or not result.passed_filters or not result.is_provisional:
            continue
        rows.append(
            {
                "track_id": track_id,
                "name": m.name,
                "seller": m.seller_name or "",
                "genre": m.primary_genre or "",
                "staleness_days": m.staleness_days if m.staleness_days is not None else "",
                "rating": m.average_user_rating if m.average_user_rating is not None else "",
                "rating_decay": round(m.rating_decay, 3) if m.rating_decay is not None else "",
                "rating_count": m.user_rating_count if m.user_rating_count is not None else "",
                "velocity_per_day": (
                    round(m.velocity.value, 3)
                    if m.velocity and m.velocity.value is not None
                    else ""
                ),
                "velocity_saturated": ("yes" if m.velocity and m.velocity.saturated else "no"),
                "installs_bucket": m.installs.bucket if m.installs else "",
                "revenue_model": m.revenue.model if m.revenue else "",
                "auto_composite": round(result.composite, 3) if result.composite else "",
                "store_url": m.store_url or "",
                # Blank columns for the human to fill in and re-import.
                **dict.fromkeys(ScoringConfig.MANUAL_DIMENSIONS, ""),
                **dict.fromkeys(config.kill_criteria, ""),
                "notes": "",
            }
        )
    rows.sort(key=lambda r: (r["auto_composite"] == "", -(r["auto_composite"] or 0)))  # type: ignore[operator]
    return rows[:limit] if limit else rows

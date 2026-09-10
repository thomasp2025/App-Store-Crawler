"""Stage 5 -- Complaint clustering.

For each shortlisted app, pull the 1-2* reviews from the trailing 12 months and extract
themes as structured JSON. The output doubles as a v1 feature spec, so the constraints
matter more than the phrasing:

  * No invented themes. Every theme must cite specific review IDs, and any ID the model
    returns that wasn't in its input is dropped -- an unverifiable theme is worse than a
    missing one, because it reads as evidence.
  * "App is broken" (crash, iOS incompatibility, login failure) is kept distinct from
    "app lacks feature X". Those imply completely different opportunities: one is a
    maintenance vacuum you can walk into, the other is a product gap you have to build.
  * Batches are chunked and merged, never truncated. Dropping the tail of a review set
    silently biases themes toward whatever sorted first.

Raw model output is persisted alongside the parsed clusters so a bad extraction can be
debugged without re-spending tokens.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from screener.config import Config
from screener.db import Database, utc_now, utc_now_iso
from screener.logging import get_logger

log = get_logger(__name__)

ComplaintKind = Literal["broken", "missing_feature", "other"]


class ComplaintTheme(BaseModel):
    theme: str = Field(description="Short label for the complaint, 3-8 words.")
    kind: ComplaintKind = Field(
        description=(
            "'broken' if the app fails at something it claims to do (crash, OS "
            "incompatibility, login failure, sync loss, data loss). "
            "'missing_feature' if the app works but lacks a capability users want. "
            "'other' for pricing, support and everything else."
        )
    )
    frequency: int = Field(description="How many reviews in this batch express this theme.")
    severity: float = Field(
        description="0-5. How badly this blocks the user's core job, 5 being unusable."
    )
    example_review_ids: list[str] = Field(
        description="Review IDs from the input that express this theme. Never invent IDs."
    )
    summary: str = Field(description="One sentence on what users actually say, in their terms.")


class ClusterExtraction(BaseModel):
    themes: list[ComplaintTheme]


SYSTEM_PROMPT = """You analyze negative app-store reviews to find what an incumbent app \
is failing at. Your output becomes the feature spec for a replacement app, so accuracy \
matters more than coverage.

Rules:
- Only report themes that actually appear in the reviews you are given. Do not infer \
themes from the app's category, name, or what apps like it usually get complained about.
- Every theme must cite the review IDs that express it. Only use IDs present in the input.
- Distinguish "the app is broken" (crashes, won't launch, iOS incompatibility, login \
fails, data or sync loss) from "the app lacks feature X" (it works, but doesn't do \
something users want). Classify each theme with `kind` accordingly. These imply very \
different opportunities, so do not blur them into one theme.
- Merge near-duplicate phrasings into one theme; do not split one complaint into several.
- If reviews are too sparse or incoherent to support any theme, return an empty list."""

MERGE_PROMPT = """You are merging complaint themes extracted from several batches of \
reviews for the same app. Combine themes that describe the same underlying complaint, \
summing their frequencies and unioning their example review IDs. Keep distinct \
complaints separate, and keep the 'broken' vs 'missing_feature' distinction intact -- \
never merge a broken-functionality theme with a missing-feature theme. Preserve the \
original wording of themes where possible. Return the merged set, most frequent first."""


@dataclass
class ClusterResult:
    track_id: int
    themes: list[dict[str, Any]] = field(default_factory=list)
    review_count: int = 0
    batch_count: int = 0
    error: str | None = None
    dropped_ids: int = 0


def _format_reviews(rows: list[Any]) -> str:
    lines = []
    for r in rows:
        title = (r["title"] or "").strip()
        body = (r["body"] or "").strip().replace("\n", " ")
        lines.append(
            f'<review id="{r["review_id"]}" rating="{r["rating"]}" '
            f'version="{r["app_version"] or "?"}" date="{str(r["updated_at"])[:10]}">'
            f"{title}. {body}</review>"
        )
    return "\n".join(lines)


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _validate_themes(
    themes: list[ComplaintTheme], valid_ids: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Drop hallucinated review IDs, then drop themes left with no evidence."""
    out: list[dict[str, Any]] = []
    dropped = 0
    for t in themes:
        kept = [rid for rid in t.example_review_ids if rid in valid_ids]
        dropped += len(t.example_review_ids) - len(kept)
        if not kept:
            log.warning("clustering.theme_unsupported", theme=t.theme)
            continue
        out.append(
            {
                "theme": t.theme,
                "kind": t.kind,
                "frequency": t.frequency,
                "severity": t.severity,
                "example_review_ids": kept,
                "summary": t.summary,
            }
        )
    return out, dropped


def cluster_app(
    db: Database, track_id: int, config: Config, *, client: Any | None = None
) -> ClusterResult:
    """Extract complaint themes for one app. Returns a result even on failure."""
    ccfg = config.clustering
    since = utc_now() - timedelta(days=ccfg.lookback_days)
    rows = db.reviews_for(track_id, since=since, max_rating=ccfg.max_star_rating)
    result = ClusterResult(track_id=track_id, review_count=len(rows))
    computed_at = utc_now_iso()

    if not rows:
        result.error = "no negative reviews in lookback window"
        log.info("clustering.skipped", track_id=track_id, reason=result.error)
        return result

    if client is None:
        client = _build_client()
        if client is None:
            result.error = "ANTHROPIC_API_KEY not set"
            return result

    batches = _chunk(rows, ccfg.max_reviews_per_batch)
    result.batch_count = len(batches)
    valid_ids = {str(r["review_id"]) for r in rows}
    raw_chunks: list[str] = []
    collected: list[ComplaintTheme] = []

    app = db.get_app(track_id)
    app_name = str(app["name"]) if app else str(track_id)

    try:
        for i, batch in enumerate(batches, start=1):
            response = client.messages.parse(
                model=ccfg.model,
                max_tokens=ccfg.max_output_tokens,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"App: {app_name}\n"
                            f"Batch {i} of {len(batches)}. "
                            f"{len(batch)} reviews rated {ccfg.max_star_rating} stars or "
                            f"below, from the last {ccfg.lookback_days} days.\n\n"
                            f"{_format_reviews(batch)}\n\n"
                            "Extract the complaint themes present in these reviews."
                        ),
                    }
                ],
                output_format=ClusterExtraction,
            )
            parsed = response.parsed_output
            raw_chunks.append(response.to_json() if hasattr(response, "to_json") else str(parsed))
            if parsed is not None:
                collected.extend(parsed.themes)
            log.info(
                "clustering.batch_done",
                track_id=track_id,
                batch=i,
                themes=len(parsed.themes) if parsed else 0,
            )

        # A merge pass, rather than truncating to whatever the first batch found.
        if len(batches) > 1 and collected:
            merged = _merge_themes(client, ccfg, app_name, collected)
            if merged is not None:
                collected = merged

    except Exception as exc:  # noqa: BLE001 - one app's failure must not kill the run
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("clustering.failed", track_id=track_id, error=result.error)
        db.record_clustering_run(
            track_id,
            computed_at,
            ccfg.model,
            len(rows),
            len(batches),
            "\n".join(raw_chunks),
            result.error,
        )
        return result

    themes, dropped = _validate_themes(collected, valid_ids)
    themes.sort(key=lambda t: (-(t["frequency"] or 0), -(t["severity"] or 0)))
    for t in themes:
        t["example_review_ids"] = t["example_review_ids"][: ccfg.max_examples_per_theme]

    result.themes = themes
    result.dropped_ids = dropped

    db.replace_clusters(track_id, computed_at, themes)
    db.record_clustering_run(
        track_id, computed_at, ccfg.model, len(rows), len(batches), "\n".join(raw_chunks), None
    )
    log.info(
        "clustering.done",
        track_id=track_id,
        themes=len(themes),
        reviews=len(rows),
        dropped_ids=dropped,
    )
    return result


def _merge_themes(
    client: Any, ccfg: Any, app_name: str, themes: list[ComplaintTheme]
) -> list[ComplaintTheme] | None:
    payload = json.dumps([t.model_dump() for t in themes], indent=1)
    try:
        response = client.messages.parse(
            model=ccfg.model,
            max_tokens=ccfg.max_output_tokens,
            system=MERGE_PROMPT,
            messages=[{"role": "user", "content": f"App: {app_name}\n\nThemes:\n{payload}"}],
            output_format=ClusterExtraction,
        )
        return response.parsed_output.themes if response.parsed_output else None
    except Exception as exc:  # noqa: BLE001
        # Keep the unmerged themes rather than losing the whole extraction.
        log.warning("clustering.merge_failed", error=str(exc))
        return None


def _build_client() -> Any | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("clustering.no_api_key")
        return None
    try:
        import anthropic
    except ImportError:
        log.error("clustering.sdk_missing", hint="pip install anthropic")
        return None
    return anthropic.Anthropic()


def run_clustering(
    db: Database, config: Config, *, track_ids: list[int], client: Any | None = None
) -> list[ClusterResult]:
    if client is None:
        client = _build_client()
    return [cluster_app(db, tid, config, client=client) for tid in track_ids]

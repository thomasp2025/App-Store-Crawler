"""Read and write config.toml from the UI.

tomlkit rather than tomllib: every threshold in config.toml carries a comment
explaining what it does and why, and a round-trip through a plain dict would throw all
of that away the first time someone moved a slider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit

from screener.config import ConfigError, load_config

# Only these are editable from the UI. Everything else in config.toml stays
# file-only -- rate limits and cache TTLs are etiquette, not tuning knobs, and a
# stray click shouldn't be able to get the crawler blocked.
EDITABLE: dict[str, set[str]] = {
    "filters": {
        "min_staleness_days",
        "min_velocity_trend",
        "max_average_user_rating",
        "min_average_user_rating",
        "min_user_rating_count",
        "max_fresh_competitors",
        "competitor_fresh_days",
        "competitor_cohort_top_rank",
        "competitor_require_same_genre",
    },
    "discovery": {"country", "search_limit", "min_rank", "max_rank"},
    "metrics": {"rating_rate", "velocity_window_days"},
    "scoring": {"rubric_version"},
}

WEIGHT_KEYS = (
    "demand_persistence",
    "incumbent_weakness",
    "monetization_ceiling",
    "capability_delta",
    "build_cost",
    "distribution_wedge",
)


def read_settings(path: Path) -> dict[str, Any]:
    doc = tomlkit.parse(path.read_text("utf-8"))
    out: dict[str, Any] = {}
    for section, keys in EDITABLE.items():
        out[section] = {k: doc[section][k] for k in keys if k in doc[section]}
    out["weights"] = {k: doc["scoring"]["weights"][k] for k in WEIGHT_KEYS}
    return out


def write_settings(path: Path, payload: dict[str, Any]) -> None:
    """Apply an edit, then validate by reloading. Never leaves a broken file behind."""
    original = path.read_text("utf-8")
    doc = tomlkit.parse(original)

    for section, keys in EDITABLE.items():
        for key, value in (payload.get(section) or {}).items():
            if key in keys:
                doc[section][key] = value

    weights = payload.get("weights") or {}
    for key, value in weights.items():
        if key in WEIGHT_KEYS:
            doc["scoring"]["weights"][key] = float(value)

    path.write_text(tomlkit.dumps(doc), "utf-8")
    try:
        load_config(path)
    except ConfigError:
        # Roll back rather than leaving the crawler unable to start.
        path.write_text(original, "utf-8")
        raise


def read_seeds(path: Path) -> str:
    return path.read_text("utf-8") if path.exists() else ""


def write_seeds(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", "utf-8")

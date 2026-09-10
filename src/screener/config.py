"""Typed access to config.toml.

Every threshold, weight and constant lives in config.toml. Nothing here invents a
default that isn't in that file -- if a key is missing we want a loud KeyError during
load, not a silent fallback that makes a tuning run unreproducible.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.toml")

# Weights are floats read from TOML; allow for representation error when summing.
_WEIGHT_SUM_TOLERANCE = 1e-6


class ConfigError(RuntimeError):
    """config.toml is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class HttpConfig:
    rate_limit_per_minute: int
    max_concurrency: int
    timeout_seconds: float
    user_agent: str
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    backoff_jitter: float
    hard_stop_after_consecutive_403: int


@dataclass(frozen=True)
class CacheConfig:
    dir: Path
    enabled: bool
    ttl_hours_lookup: int
    ttl_hours_reviews: int
    ttl_hours_search: int

    def ttl_hours_for(self, kind: str) -> int:
        try:
            return int(getattr(self, f"ttl_hours_{kind}"))
        except AttributeError as exc:  # pragma: no cover - programmer error
            raise ConfigError(f"no cache TTL configured for kind {kind!r}") from exc


@dataclass(frozen=True)
class DiscoveryConfig:
    country: str
    search_limit: int
    lookup_batch_size: int
    min_rank: int
    max_rank: int
    seeds_file: Path


@dataclass(frozen=True)
class ReviewsConfig:
    country: str
    max_pages: int
    page_size_hint: int


@dataclass(frozen=True)
class MetricsConfig:
    rating_rate: float
    velocity_window_days: int
    min_reviews_for_span_velocity: int
    saturation_span_days: int


@dataclass(frozen=True)
class FiltersConfig:
    min_staleness_days: int
    min_velocity_trend: float
    max_average_user_rating: float
    min_average_user_rating: float
    min_user_rating_count: int
    max_fresh_competitors: int
    competitor_fresh_days: int
    competitor_cohort_top_rank: int
    competitor_require_same_genre: bool


@dataclass(frozen=True)
class ScoringConfig:
    rubric_version: str
    weights: dict[str, float]
    velocity_breakpoints: list[float]
    trend_bonus_at: float
    trend_bonus: float
    decay_breakpoints: list[float]
    review_decay_breakpoints: list[float]
    staleness_breakpoints: list[float]
    complaint_density_breakpoints: list[float]
    decay_weight: float
    review_decay_weight: float
    staleness_weight: float
    complaint_density_weight: float

    # Dimensions the crawler can compute. The rest require human judgment.
    AUTO_DIMENSIONS = ("demand_persistence", "incumbent_weakness")
    MANUAL_DIMENSIONS = (
        "monetization_ceiling",
        "capability_delta",
        "build_cost",
        "distribution_wedge",
    )


@dataclass(frozen=True)
class ClusteringConfig:
    model: str
    max_reviews_per_batch: int
    max_output_tokens: int
    lookback_days: int
    max_star_rating: int
    max_examples_per_theme: int


@dataclass(frozen=True)
class ReportConfig:
    shortlist_limit: int
    sparkline_buckets: int


@dataclass(frozen=True)
class Config:
    http: HttpConfig
    cache: CacheConfig
    db_path: Path
    discovery: DiscoveryConfig
    reviews: ReviewsConfig
    metrics: MetricsConfig
    filters: FiltersConfig
    scoring: ScoringConfig
    kill_criteria: list[str]
    clustering: ClusteringConfig
    report: ReportConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate config.toml."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    try:
        scoring_raw = raw["scoring"]
        weights = dict(scoring_raw["weights"])
        demand = scoring_raw["demand_persistence"]
        weakness = scoring_raw["incumbent_weakness"]

        cfg = Config(
            http=HttpConfig(**raw["http"]),
            cache=CacheConfig(
                dir=Path(raw["cache"]["dir"]),
                enabled=raw["cache"]["enabled"],
                ttl_hours_lookup=raw["cache"]["ttl_hours_lookup"],
                ttl_hours_reviews=raw["cache"]["ttl_hours_reviews"],
                ttl_hours_search=raw["cache"]["ttl_hours_search"],
            ),
            db_path=Path(raw["db"]["path"]),
            discovery=DiscoveryConfig(
                country=raw["discovery"]["country"],
                search_limit=raw["discovery"]["search_limit"],
                lookup_batch_size=raw["discovery"]["lookup_batch_size"],
                min_rank=raw["discovery"]["min_rank"],
                max_rank=raw["discovery"]["max_rank"],
                seeds_file=Path(raw["discovery"]["seeds_file"]),
            ),
            reviews=ReviewsConfig(**raw["reviews"]),
            metrics=MetricsConfig(**raw["metrics"]),
            filters=FiltersConfig(**raw["filters"]),
            scoring=ScoringConfig(
                rubric_version=scoring_raw["rubric_version"],
                weights=weights,
                velocity_breakpoints=list(demand["velocity_breakpoints"]),
                trend_bonus_at=demand["trend_bonus_at"],
                trend_bonus=demand["trend_bonus"],
                decay_breakpoints=list(weakness["decay_breakpoints"]),
                review_decay_breakpoints=list(weakness["review_decay_breakpoints"]),
                staleness_breakpoints=list(weakness["staleness_breakpoints"]),
                complaint_density_breakpoints=list(weakness["complaint_density_breakpoints"]),
                decay_weight=weakness["decay_weight"],
                review_decay_weight=weakness["review_decay_weight"],
                staleness_weight=weakness["staleness_weight"],
                complaint_density_weight=weakness["complaint_density_weight"],
            ),
            kill_criteria=list(raw["kill_criteria"]["flags"]),
            clustering=ClusteringConfig(**raw["clustering"]),
            report=ReportConfig(**raw["report"]),
            raw=raw,
        )
    except KeyError as exc:
        raise ConfigError(f"missing required key in {path}: {exc}") from exc
    except TypeError as exc:
        raise ConfigError(f"malformed section in {path}: {exc}") from exc

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    total = sum(cfg.scoring.weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ConfigError(f"scoring.weights must sum to 1.0, got {total:.6f}")

    expected = set(ScoringConfig.AUTO_DIMENSIONS) | set(ScoringConfig.MANUAL_DIMENSIONS)
    if set(cfg.scoring.weights) != expected:
        missing = expected - set(cfg.scoring.weights)
        extra = set(cfg.scoring.weights) - expected
        raise ConfigError(f"scoring.weights mismatch (missing={missing}, unexpected={extra})")

    sub = (
        cfg.scoring.decay_weight
        + cfg.scoring.review_decay_weight
        + cfg.scoring.staleness_weight
        + cfg.scoring.complaint_density_weight
    )
    if abs(sub - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ConfigError(f"scoring.incumbent_weakness sub-weights must sum to 1.0, got {sub:.6f}")

    for name in (
        "velocity_breakpoints",
        "decay_breakpoints",
        "review_decay_breakpoints",
        "staleness_breakpoints",
        "complaint_density_breakpoints",
    ):
        bps = getattr(cfg.scoring, name)
        if len(bps) != 5:
            raise ConfigError(f"scoring.{name} must have exactly 5 breakpoints, got {len(bps)}")
        if list(bps) != sorted(bps):
            raise ConfigError(f"scoring.{name} must be ascending")

    if cfg.http.rate_limit_per_minute <= 0:
        raise ConfigError("http.rate_limit_per_minute must be positive")
    if cfg.filters.min_average_user_rating >= cfg.filters.max_average_user_rating:
        raise ConfigError("filters.min_average_user_rating must be below max_average_user_rating")

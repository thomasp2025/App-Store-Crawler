"""Typer CLI. Every command loads config.toml; nothing is hardcoded here."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from rich.console import Console

from screener import reports
from screener.config import Config, ConfigError, load_config
from screener.db import Database
from screener.http.cache import DiskCache
from screener.http.client import HardStop, ITunesClient
from screener.logging import configure_logging
from screener.pipeline import clustering, discovery, reviews, scoring, snapshot
from screener.sources.itunes import ITunesSource

app = typer.Typer(
    help="App Store opportunity screener — find stale apps in live markets.",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(help="Inspect and clear the on-disk response cache.")
report_app = typer.Typer(help="Emit Markdown/CSV reports.")
app.add_typer(cache_app, name="cache")
app.add_typer(report_app, name="report")

console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.toml.")]
NoCacheOpt = Annotated[bool, typer.Option("--no-cache", help="Bypass the disk cache.")]
LogLevelOpt = Annotated[str, typer.Option("--log-level", help="DEBUG/INFO/WARNING/ERROR.")]


def _load(config_path: Path, log_level: str = "INFO", json_logs: bool = False) -> Config:
    load_dotenv()
    configure_logging(log_level, json_output=json_logs)
    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _client(config: Config, no_cache: bool) -> ITunesClient:
    cache = DiskCache(config.cache.dir, enabled=config.cache.enabled and not no_cache)
    return ITunesClient(config, cache=cache)


def _ids_arg(track_ids: list[int] | None) -> list[int] | None:
    return list(track_ids) if track_ids else None


# --------------------------------------------------------------------------- setup


@app.command()
def init(config: ConfigOpt = Path("config.toml")) -> None:
    """Create the database and apply the schema. Safe to re-run."""
    cfg = _load(config)
    with Database(cfg.db_path) as db:
        db.migrate()
        seeds = discovery.load_seeds(cfg.discovery.seeds_file)
        for term in seeds:
            db.upsert_keyword(term, source="seed")
    console.print(f"[green]Initialised[/green] {cfg.db_path} with {len(seeds)} seed keywords.")


@app.command("verify-endpoints")
def verify_endpoints(
    config: ConfigOpt = Path("config.toml"),
    track_id: Annotated[int, typer.Option(help="Known-good app ID to probe with.")] = 320606217,
) -> None:
    """Probe Search, Lookup and the reviews feed; report what actually works.

    Apple has broken these before. Run this before trusting a crawl.
    """
    cfg = _load(config)

    async def _run() -> None:
        async with _client(cfg, no_cache=True) as client:
            source = ITunesSource(client, cfg)
            console.print("[bold]Probing endpoints…[/bold]")

            results = await source.search("sleep tracker", use_cache=False)
            console.print(
                f"  search : {'[green]OK[/green]' if results else '[red]FAIL[/red]'} "
                f"— {len(results)} results"
            )

            looked = await source.lookup([track_id], use_cache=False)
            console.print(
                f"  lookup : {'[green]OK[/green]' if looked else '[red]FAIL[/red]'} "
                f"— {len(looked)} records"
            )
            if looked:
                a = looked[0]
                console.print(
                    f"           {a.track_name} · v{a.version} · "
                    f"shipped {a.current_version_release_date}"
                )

            revs = await source.reviews(track_id, use_cache=False, max_pages=2)
            console.print(
                f"  reviews: {'[green]OK[/green]' if revs else '[red]FAIL[/red]'} "
                f"— {len(revs)} reviews over 2 pages"
            )
            if revs:
                console.print(f"           newest {revs[0].updated_at} " f"({revs[0].rating}★)")
            console.print("\nSee [cyan]docs/endpoints.md[/cyan] for the recorded shapes.")

    asyncio.run(_run())


# ----------------------------------------------------------------------- pipeline


@app.command()
def discover(
    config: ConfigOpt = Path("config.toml"),
    term: Annotated[
        list[str] | None,
        typer.Option("--term", "-t", help="Search this term instead of the seed file."),
    ] = None,
    no_cache: NoCacheOpt = False,
    log_level: LogLevelOpt = "INFO",
) -> None:
    """Stage 1 — search seed keywords and record every app found, with rank."""
    cfg = _load(config, log_level)
    with Database(cfg.db_path) as db:
        db.migrate()

        async def _run() -> Any:
            async with _client(cfg, no_cache) as client:
                return await discovery.run_discovery(
                    db,
                    ITunesSource(client, cfg),
                    cfg,
                    terms=list(term) if term else None,
                    use_cache=not no_cache,
                )

        try:
            result = asyncio.run(_run())
        except HardStop as exc:
            console.print(f"[red]Hard stop:[/red] {exc}")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]Discovery:[/green] {result.terms_searched} terms, "
        f"{result.apps_seen} hits, {result.new_apps} new apps."
    )


@app.command("snapshot")
def snapshot_cmd(
    config: ConfigOpt = Path("config.toml"),
    track_id: Annotated[
        list[int] | None, typer.Option("--track-id", help="Snapshot only these apps.")
    ] = None,
    no_cache: NoCacheOpt = False,
    log_level: LogLevelOpt = "INFO",
    json_logs: Annotated[
        bool, typer.Option("--json-logs", help="JSON log lines, for cron.")
    ] = False,
) -> None:
    """Capture today's snapshot for every tracked app. Idempotent — run it daily.

    This is the command to put on a cron. Scoring can be backfilled; missed days cannot.
    """
    cfg = _load(config, log_level, json_logs)
    with Database(cfg.db_path) as db:
        db.migrate()

        async def _run() -> Any:
            async with _client(cfg, no_cache) as client:
                return await snapshot.run_snapshot(
                    db,
                    ITunesSource(client, cfg),
                    cfg,
                    track_ids=_ids_arg(track_id),
                    use_cache=not no_cache,
                )

        try:
            result = asyncio.run(_run())
        except HardStop as exc:
            console.print(f"[red]Hard stop:[/red] {exc}")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]Snapshot:[/green] {result.captured}/{result.requested} captured "
        f"({result.new_rows} new rows, {result.updated_rows} updated)."
    )
    if result.missing:
        console.print(
            f"[yellow]{len(result.missing)} apps no longer returned by lookup "
            f"(delisted?):[/yellow] {result.missing[:10]}"
        )


@app.command("fetch-reviews")
def fetch_reviews(
    config: ConfigOpt = Path("config.toml"),
    track_id: Annotated[list[int] | None, typer.Option("--track-id")] = None,
    no_cache: NoCacheOpt = False,
    max_pages: Annotated[int | None, typer.Option(help="Override page depth (max 10).")] = None,
    log_level: LogLevelOpt = "INFO",
) -> None:
    """Pull the ~500 most recent reviews per app. Deduped on review ID."""
    cfg = _load(config, log_level)
    with Database(cfg.db_path) as db:
        db.migrate()

        async def _run() -> Any:
            async with _client(cfg, no_cache) as client:
                return await reviews.run_review_ingest(
                    db,
                    ITunesSource(client, cfg),
                    cfg,
                    track_ids=_ids_arg(track_id),
                    use_cache=not no_cache,
                    max_pages=max_pages,
                )

        try:
            result = asyncio.run(_run())
        except HardStop as exc:
            console.print(f"[red]Hard stop:[/red] {exc}")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]Reviews:[/green] {result.apps} apps, {result.fetched} fetched, "
        f"{result.inserted} new, {result.updated} edited."
    )


@app.command("score")
def score_cmd(
    config: ConfigOpt = Path("config.toml"),
    track_id: Annotated[list[int] | None, typer.Option("--track-id")] = None,
    log_level: LogLevelOpt = "INFO",
) -> None:
    """Stages 2–4 — apply hard filters and auto-score the computable dimensions."""
    cfg = _load(config, log_level)
    with Database(cfg.db_path) as db:
        db.migrate()
        result = scoring.run_auto_scoring(db, cfg, track_ids=_ids_arg(track_id))
    console.print(
        f"[green]Scored:[/green] {result.scored} apps — {result.passed} passed, "
        f"{result.rejected} rejected, {result.needs_manual} awaiting manual scoring."
    )
    if result.needs_manual:
        console.print(
            "Next: [cyan]screener queue --out review.csv[/cyan], fill it in, then "
            "[cyan]screener import-scores review.csv[/cyan]."
        )


@app.command()
def queue(
    config: ConfigOpt = Path("config.toml"),
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write CSV here.")] = None,
    limit: Annotated[int | None, typer.Option(help="Cap the queue length.")] = None,
) -> None:
    """Emit the manual review queue as CSV — apps that passed filters, awaiting judgment."""
    cfg = _load(config)
    with Database(cfg.db_path) as db:
        db.migrate()
        rows = scoring.review_queue_rows(db, cfg, limit=limit)
    if not rows:
        console.print("[yellow]Nothing awaiting manual scoring.[/yellow]")
        raise typer.Exit(0)
    text = reports.rows_to_csv(rows)
    if out:
        out.write_text(text, "utf-8")
        console.print(f"[green]Wrote[/green] {len(rows)} rows to {out}")
        console.print(
            "Fill in the manual dimension columns (0–5) and any kill flags "
            "(1/0), then re-import."
        )
    else:
        console.print(text)


@app.command("import-scores")
def import_scores(
    path: Annotated[
        Path, typer.Argument(help="CSV produced by `queue`, with manual columns filled.")
    ],
    config: ConfigOpt = Path("config.toml"),
) -> None:
    """Import manual dimension scores and kill flags from CSV."""
    cfg = _load(config)
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with Database(cfg.db_path) as db:
        db.migrate()
        count = scoring.import_manual_scores(db, rows, cfg)
        result = scoring.run_auto_scoring(db, cfg)
    console.print(
        f"[green]Imported[/green] {count} rows; rescored {result.scored} apps "
        f"({result.passed} passing)."
    )


@app.command("set-score")
def set_score(
    track_id: Annotated[int, typer.Argument()],
    config: ConfigOpt = Path("config.toml"),
    monetization_ceiling: Annotated[float | None, typer.Option(min=0, max=5)] = None,
    capability_delta: Annotated[float | None, typer.Option(min=0, max=5)] = None,
    build_cost: Annotated[
        float | None, typer.Option(min=0, max=5, help="Inverted — 5 means cheap to build.")
    ] = None,
    distribution_wedge: Annotated[float | None, typer.Option(min=0, max=5)] = None,
    kill: Annotated[
        list[str] | None,
        typer.Option("--kill", help="Set a kill flag, e.g. --kill native_os_feature."),
    ] = None,
    notes: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Score one app's manual dimensions without a CSV round-trip."""
    cfg = _load(config)
    with Database(cfg.db_path) as db:
        db.migrate()
        if db.get_app(track_id) is None:
            console.print(f"[red]No app with track_id {track_id}.[/red]")
            raise typer.Exit(1)
        db.upsert_manual_score(
            track_id,
            cfg.scoring.rubric_version,
            {
                "monetization_ceiling": monetization_ceiling,
                "capability_delta": capability_delta,
                "build_cost": build_cost,
                "distribution_wedge": distribution_wedge,
            },
            notes=notes,
        )
        for flag in kill or []:
            if flag not in cfg.kill_criteria:
                console.print(
                    f"[red]Unknown kill flag {flag!r}.[/red] "
                    f"Valid: {', '.join(cfg.kill_criteria)}"
                )
                raise typer.Exit(1)
            db.set_kill_flag(track_id, flag, True)
        scoring.run_auto_scoring(db, cfg, track_ids=[track_id])
        result, _, _ = scoring.score_app(db, track_id, cfg)
    console.print(
        f"[green]Scored[/green] {track_id}: composite " f"{result.composite:.2f}"
        if result.composite
        else "composite —"
    )
    if result.manual_missing:
        console.print(f"[yellow]Still missing:[/yellow] {', '.join(result.manual_missing)}")


@app.command()
def cluster(
    track_id: Annotated[
        list[int] | None, typer.Argument(help="Apps to cluster. " "Defaults to the shortlist.")
    ] = None,
    config: ConfigOpt = Path("config.toml"),
    limit: Annotated[int, typer.Option(help="Cap how many shortlisted apps to cluster.")] = 10,
    log_level: LogLevelOpt = "INFO",
) -> None:
    """Stage 5 — cluster 1–2★ reviews into themes via the Anthropic API."""
    cfg = _load(config, log_level)
    with Database(cfg.db_path) as db:
        db.migrate()
        if track_id:
            ids = list(track_id)
        else:
            ids = [
                int(r["track_id"])
                for r in db.latest_scores(cfg.scoring.rubric_version, passed_only=True, limit=limit)
            ]
            if not ids:
                console.print(
                    "[yellow]Nothing on the shortlist. Pass track IDs explicitly.[/yellow]"
                )
                raise typer.Exit(0)
        results = clustering.run_clustering(db, cfg, track_ids=ids)

    for r in results:
        if r.error:
            console.print(f"[yellow]{r.track_id}:[/yellow] {r.error}")
        else:
            extra = f", dropped {r.dropped_ids} invented IDs" if r.dropped_ids else ""
            console.print(
                f"[green]{r.track_id}:[/green] {len(r.themes)} themes from "
                f"{r.review_count} reviews ({r.batch_count} batches{extra})"
            )


# ------------------------------------------------------------------------ reports


@report_app.command("shortlist")
def report_shortlist(
    config: ConfigOpt = Path("config.toml"),
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
    limit: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Ranked Markdown table of candidates that cleared the filters."""
    cfg = _load(config, "WARNING")
    with Database(cfg.db_path) as db:
        db.migrate()
        text = reports.shortlist_markdown(db, cfg, limit=limit)
    _emit(text, out)


@report_app.command("app")
def report_app_cmd(
    track_id: Annotated[int, typer.Argument()],
    config: ConfigOpt = Path("config.toml"),
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
) -> None:
    """One-page dossier: metrics, sparkline, complaint clusters, competitor set."""
    cfg = _load(config, "WARNING")
    with Database(cfg.db_path) as db:
        db.migrate()
        text = reports.app_dossier(db, track_id, cfg)
    _emit(text, out)


@report_app.command("keywords")
def report_keywords(
    config: ConfigOpt = Path("config.toml"),
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
) -> None:
    """Yield per keyword, so unproductive seeds get pruned."""
    cfg = _load(config, "WARNING")
    with Database(cfg.db_path) as db:
        db.migrate()
        text = reports.keyword_yield_markdown(db)
    _emit(text, out)


def _emit(text: str, out: Path | None) -> None:
    if out:
        out.write_text(text, "utf-8")
        console.print(f"[green]Wrote[/green] {out}")
    else:
        print(text)


# -------------------------------------------------------------------------- cache


@cache_app.command("clear")
def cache_clear(
    config: ConfigOpt = Path("config.toml"),
    kind: Annotated[str | None, typer.Option(help="search | lookup | reviews.")] = None,
) -> None:
    """Delete cached responses. Saves hours during development."""
    cfg = _load(config, "WARNING")
    cache = DiskCache(cfg.cache.dir, enabled=True)
    removed = cache.clear(kind)
    console.print(
        f"[green]Cleared[/green] {removed} cached responses"
        f"{f' of kind {kind}' if kind else ''}."
    )


@cache_app.command("stats")
def cache_stats(config: ConfigOpt = Path("config.toml")) -> None:
    """Show how many responses are cached, by kind."""
    cfg = _load(config, "WARNING")
    stats = DiskCache(cfg.cache.dir, enabled=True).stats()
    if not stats:
        console.print("Cache is empty.")
        return
    for kind, count in stats.items():
        console.print(f"  {kind:<10} {count}")


@app.command()
def status(config: ConfigOpt = Path("config.toml")) -> None:
    """Database counts and collection coverage."""
    cfg = _load(config, "WARNING")
    with Database(cfg.db_path) as db:
        db.migrate()
        counts = db.counts()
        dates = db.snapshot_dates()
    console.print(f"[bold]{cfg.db_path}[/bold]")
    for table, n in counts.items():
        console.print(f"  {table:<20} {n}")
    if dates:
        console.print(f"\n  snapshot days       {len(dates)} " f"({dates[0]} → {dates[-1]})")
        if len(dates) < 180:
            console.print(
                f"  [yellow]velocity_trend needs ~180 days of history; "
                f"{len(dates)} so far.[/yellow]"
            )
    else:
        console.print("\n  [yellow]No snapshots yet. Run `screener snapshot`.[/yellow]")


@app.command()
def serve(
    config: ConfigOpt = Path("config.toml"),
    host: Annotated[str, typer.Option(help="Bind address. Localhost by default.")] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
    log_level: LogLevelOpt = "WARNING",
) -> None:
    """Open the local web UI — tune filters, sort/filter apps, view dossiers.

    Reads and writes the real config.toml and the real database, so it binds to
    localhost only. Don't put it on a network.
    """
    cfg = _load(config, log_level)
    try:
        import uvicorn

        from screener.web import create_app
    except ImportError as exc:
        console.print(
            "[red]UI dependencies missing.[/red] Install with: "
            "[cyan]pip install -e '.[ui]'[/cyan]"
        )
        raise typer.Exit(1) from exc

    with Database(cfg.db_path) as db:
        db.migrate()
    console.print(f"[green]Screener UI[/green] → http://{host}:{port}")
    uvicorn.run(create_app(config.resolve()), host=host, port=port, log_level="warning")


@app.command("run-daily")
def run_daily(
    config: ConfigOpt = Path("config.toml"),
    skip_discovery: Annotated[bool, typer.Option("--skip-discovery")] = False,
    log_level: LogLevelOpt = "INFO",
    json_logs: Annotated[bool, typer.Option("--json-logs")] = False,
) -> None:
    """The cron entrypoint: discover → snapshot → reviews → score."""
    cfg = _load(config, log_level, json_logs)
    with Database(cfg.db_path) as db:
        db.migrate()

        async def _run() -> None:
            async with _client(cfg, no_cache=False) as client:
                source = ITunesSource(client, cfg)
                if not skip_discovery:
                    await discovery.run_discovery(db, source, cfg)
                await snapshot.run_snapshot(db, source, cfg)
                await reviews.run_review_ingest(db, source, cfg)

        try:
            asyncio.run(_run())
        except HardStop as exc:
            console.print(f"[red]Hard stop:[/red] {exc}")
            raise typer.Exit(1) from exc
        result = scoring.run_auto_scoring(db, cfg)
    console.print(
        f"[green]Daily run complete.[/green] {result.scored} scored, " f"{result.passed} passing."
    )


if __name__ == "__main__":
    app()

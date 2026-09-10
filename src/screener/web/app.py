"""Local FastAPI app backing the screening UI.

Binds to localhost only. This reads and writes the real config.toml and the real
SQLite DB, so it is deliberately not something to expose on a network.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from screener import reports
from screener.config import ConfigError, load_config
from screener.db import Database
from screener.http.cache import DiskCache
from screener.http.client import HardStop, ITunesClient
from screener.logging import get_logger
from screener.pipeline import discovery, scoring, snapshot
from screener.pipeline import reviews as reviews_stage
from screener.pipeline.context import build_metrics
from screener.sources.itunes import ITunesSource
from screener.web import settings_io

log = get_logger(__name__)
STATIC = Path(__file__).parent / "static"


class JobRunner:
    """Runs one pipeline stage at a time in a background thread.

    Single-slot on purpose: the crawler is rate-limited globally and SQLite is a
    single writer, so two concurrent runs would fight over both.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "running": False,
            "stage": None,
            "log": [],
            "error": None,
            "finished": None,
        }

    @property
    def running(self) -> bool:
        return bool(self.state["running"])

    def start(self, stage: str, fn: Any) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {
                "running": True,
                "stage": stage,
                "log": [f"started {stage}"],
                "error": None,
                "finished": None,
            }

        def _run() -> None:
            try:
                message = fn()
                self.state["log"].append(message)
                self.state["finished"] = message
            except HardStop as exc:
                self.state["error"] = f"Apple is refusing requests — {exc}"
            except Exception as exc:  # noqa: BLE001 - surface anything to the UI
                self.state["error"] = f"{type(exc).__name__}: {exc}"
                log.error("web.job_failed", stage=stage, error=str(exc))
            finally:
                self.state["running"] = False

        threading.Thread(target=_run, daemon=True).start()
        return True


def _row_payload(db: Database, cfg: Any, row: Any) -> dict[str, Any]:
    m = json.loads(row["metrics_json"] or "{}")
    manual = db.manual_score(int(row["track_id"]), cfg.scoring.rubric_version)
    return {
        "track_id": int(row["track_id"]),
        "name": row["name"],
        "seller": row["seller_name"],
        "genre": row["primary_genre"],
        "store_url": row["store_url"],
        "staleness_days": m.get("staleness_days"),
        "rating": m.get("average_user_rating"),
        "rating_count": m.get("user_rating_count"),
        "review_decay": m.get("review_rating_decay"),
        "velocity": m.get("velocity"),
        "velocity_saturated": m.get("velocity_saturated"),
        "velocity_trend": m.get("velocity_trend"),
        "complaint_density": m.get("complaint_density"),
        "installs": m.get("installs_bucket"),
        "revenue_model": m.get("revenue_model"),
        "review_count": m.get("review_count"),
        "demand_persistence": row["demand_persistence"],
        "incumbent_weakness": row["incumbent_weakness"],
        "composite": row["composite"],
        "passed": bool(row["passed_filters"]),
        "kill_reason": row["kill_reason"],
        "scored_manually": manual is not None,
    }


def _normalise_reason(reason: str | None) -> list[str]:
    """Collapse a kill_reason string into stable category labels for charting."""
    if not reason:
        return []
    out = []
    for part in reason.split("; "):
        if part.startswith("kill:"):
            out.append("manual kill flag")
        elif "staleness" in part:
            out.append("too fresh")
        elif "velocity_trend" in part:
            out.append("market dying")
        elif "users satisfied" in part:
            out.append("users satisfied")
        elif "broken market" in part:
            out.append("rating too low")
        elif "prove demand" in part:
            out.append("too few ratings")
        elif "actively served" in part:
            out.append("competitors active")
        else:
            out.append(re.sub(r"[\d.]+", "N", part)[:40])
    return out


def create_app(config_path: Path) -> FastAPI:
    api = FastAPI(title="App Store Screener", docs_url=None, redoc_url=None)
    jobs = JobRunner()

    def cfg() -> Any:
        return load_config(config_path)

    def open_db() -> Database:
        database = Database(cfg().db_path)
        database.migrate()
        return database

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @api.get("/api/apps")
    def list_apps(
        passed: str = Query("all"),
        q: str = Query(""),
        genre: str = Query(""),
        limit: int = Query(2000, le=10000),
    ) -> JSONResponse:
        c = cfg()
        db = open_db()
        try:
            rows = db.latest_scores(c.scoring.rubric_version)
            payload = [_row_payload(db, c, r) for r in rows]
        finally:
            db.close()

        if passed == "passed":
            payload = [p for p in payload if p["passed"]]
        elif passed == "rejected":
            payload = [p for p in payload if not p["passed"]]
        if genre:
            payload = [p for p in payload if (p["genre"] or "") == genre]
        if q:
            needle = q.lower()
            payload = [
                p
                for p in payload
                if needle in (p["name"] or "").lower() or needle in (p["seller"] or "").lower()
            ]
        genres = sorted({p["genre"] for p in payload if p["genre"]})
        return JSONResponse({"rows": payload[:limit], "total": len(payload), "genres": genres})

    @api.get("/api/stats")
    def stats() -> JSONResponse:
        c = cfg()
        db = open_db()
        try:
            counts = db.counts()
            dates = db.snapshot_dates()
            rows = db.latest_scores(c.scoring.rubric_version)
            reasons: Counter[str] = Counter()
            for r in rows:
                for label in _normalise_reason(r["kill_reason"]):
                    reasons[label] += 1
            passed = sum(1 for r in rows if r["passed_filters"])
        finally:
            db.close()
        return JSONResponse(
            {
                "counts": counts,
                "snapshot_days": len(dates),
                "first_snapshot": dates[0] if dates else None,
                "last_snapshot": dates[-1] if dates else None,
                "scored": len(rows),
                "passed": passed,
                "rejection_reasons": reasons.most_common(),
                "rubric_version": c.scoring.rubric_version,
            }
        )

    @api.get("/api/app/{track_id}")
    def app_detail(track_id: int) -> JSONResponse:
        c = cfg()
        db = open_db()
        try:
            m = build_metrics(db, track_id, c)
            if m is None:
                raise HTTPException(404, "unknown app")
            result, _, verdict = scoring.score_app(db, track_id, c)
            snaps = db.snapshots(track_id)
            clusters = [
                {**dict(row), "example_review_ids": json.loads(row["example_review_ids"] or "[]")}
                for row in db.latest_clusters(track_id)
            ]
            recent = [dict(r) for r in db.reviews_for(track_id)[:20]]
            dossier = reports.app_dossier(db, track_id, c)
        finally:
            db.close()
        return JSONResponse(
            {
                "track_id": track_id,
                "name": m.name,
                "seller": m.seller_name,
                "genre": m.primary_genre,
                "store_url": m.store_url,
                "metrics": m.to_json(),
                "notes": m.notes,
                "dimensions": result.dimensions,
                "composite": result.composite,
                "passed": result.passed_filters,
                "kill_reason": result.kill_reason,
                "manual_missing": result.manual_missing,
                "filter_reasons": verdict.reasons if verdict else [],
                "filter_skipped": verdict.skipped if verdict else [],
                "series": [
                    {
                        "date": s["captured_date"],
                        "rating_count": s["user_rating_count"],
                        "rating": s["average_user_rating"],
                    }
                    for s in snaps
                ],
                "clusters": clusters,
                "reviews": recent,
                "dossier_markdown": dossier,
            }
        )

    @api.get("/api/settings")
    def get_settings() -> JSONResponse:
        c = cfg()
        return JSONResponse(
            {
                "settings": settings_io.read_settings(config_path),
                "seeds": settings_io.read_seeds(c.discovery.seeds_file),
                "kill_criteria": c.kill_criteria,
            }
        )

    @api.post("/api/settings")
    def post_settings(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        try:
            settings_io.write_settings(config_path, payload)
        except ConfigError as exc:
            raise HTTPException(400, f"invalid settings, rolled back: {exc}") from exc
        if "seeds" in payload:
            settings_io.write_seeds(cfg().discovery.seeds_file, str(payload["seeds"]))
        return JSONResponse({"ok": True})

    @api.post("/api/score")
    def rescore() -> JSONResponse:
        """Re-apply filters and scoring under the current settings. No network."""
        c = cfg()
        db = open_db()
        try:
            result = scoring.run_auto_scoring(db, c)
        finally:
            db.close()
        return JSONResponse(
            {"scored": result.scored, "passed": result.passed, "rejected": result.rejected}
        )

    @api.post("/api/manual/{track_id}")
    def set_manual(track_id: int, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        c = cfg()
        db = open_db()
        try:
            values = {
                k: (float(v) if v not in (None, "") else None)
                for k, v in (payload.get("scores") or {}).items()
            }
            db.upsert_manual_score(
                track_id, c.scoring.rubric_version, values, notes=payload.get("notes") or None
            )
            for flag, value in (payload.get("kill_flags") or {}).items():
                if flag in c.kill_criteria:
                    db.set_kill_flag(track_id, flag, bool(value))
            scoring.run_auto_scoring(db, c, track_ids=[track_id])
        finally:
            db.close()
        return JSONResponse({"ok": True})

    @api.get("/api/job")
    def job_state() -> JSONResponse:
        return JSONResponse(jobs.state)

    @api.post("/api/run/{stage}")
    def run_stage(stage: str, payload: dict[str, Any] = Body(default={})) -> JSONResponse:
        if stage not in {"discover", "snapshot", "reviews"}:
            raise HTTPException(400, f"unknown stage {stage}")
        terms = payload.get("terms") or None
        track_ids = payload.get("track_ids") or None

        def work() -> str:
            c = load_config(config_path)
            db = Database(c.db_path)
            db.migrate()
            try:

                async def _go() -> str:
                    cache = DiskCache(c.cache.dir, enabled=c.cache.enabled)
                    async with ITunesClient(c, cache=cache) as client:
                        src = ITunesSource(client, c)
                        if stage == "discover":
                            r = await discovery.run_discovery(db, src, c, terms=terms)
                            return (
                                f"{r.terms_searched} terms searched, {r.apps_seen} hits, "
                                f"{r.new_apps} new apps"
                            )
                        if stage == "snapshot":
                            r2 = await snapshot.run_snapshot(db, src, c, track_ids=track_ids)
                            return (
                                f"{r2.captured}/{r2.requested} captured "
                                f"({r2.new_rows} new, {r2.updated_rows} updated)"
                            )
                        r3 = await reviews_stage.run_review_ingest(db, src, c, track_ids=track_ids)
                        return (
                            f"{r3.apps} apps, {r3.fetched} reviews fetched, " f"{r3.inserted} new"
                        )

                message = asyncio.run(_go())
                scoring.run_auto_scoring(db, c)
                return message + " — rescored"
            finally:
                db.close()

        if not jobs.start(stage, work):
            raise HTTPException(409, "a run is already in progress")
        return JSONResponse({"started": stage})

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api

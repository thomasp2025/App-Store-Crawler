"""Web layer. Settings writes touch the real config.toml, so rollback matters most."""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from screener.config import ConfigError, load_config
from screener.web import create_app, settings_io


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """A real config.toml copy, pointed at a temp DB, cwd'd into tmp_path."""
    text = pathlib.Path("config.toml").read_text()
    text = text.replace('path = "data/screener.db"', f'path = "{tmp_path / "t.db"}"')
    text = text.replace('dir = "data/cache"', f'dir = "{tmp_path / "cache"}"')
    text = text.replace('seeds_file = "data/seeds.txt"', f'seeds_file = "{tmp_path / "seeds.txt"}"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.chdir(tmp_path)
    return path


@pytest.fixture
def client(cfg_file):
    return TestClient(create_app(cfg_file))


class TestSettingsIO:
    def test_roundtrip_preserves_comments(self, cfg_file):
        before = cfg_file.read_text()
        assert "# Undocumented public endpoints" in before
        settings_io.write_settings(cfg_file, {"filters": {"min_staleness_days": 600}})
        after = cfg_file.read_text()
        # The comments explain every threshold; a dict round-trip would erase them.
        assert "# Undocumented public endpoints" in after
        assert "highest-signal" in after or "docs/endpoints.md" in after
        assert load_config(cfg_file).filters.min_staleness_days == 600

    def test_rejects_and_rolls_back_invalid_weights(self, cfg_file):
        before = cfg_file.read_text()
        with pytest.raises(ConfigError):
            settings_io.write_settings(cfg_file, {"weights": {"demand_persistence": 0.9}})
        # Must not leave a config the crawler can't start with.
        assert cfg_file.read_text() == before
        assert load_config(cfg_file)

    def test_ignores_keys_outside_the_editable_allowlist(self, cfg_file):
        settings_io.write_settings(
            cfg_file, {"http": {"rate_limit_per_minute": 9999}, "filters": {}}
        )
        # Rate limits are etiquette, not a tuning knob -- the UI must not raise them.
        assert load_config(cfg_file).http.rate_limit_per_minute == 15

    def test_reads_only_editable_sections(self, cfg_file):
        s = settings_io.read_settings(cfg_file)
        assert "min_staleness_days" in s["filters"]
        assert "http" not in s
        assert abs(sum(s["weights"].values()) - 1.0) < 1e-9

    def test_seeds_roundtrip(self, tmp_path):
        p = tmp_path / "seeds.txt"
        settings_io.write_seeds(p, "one\ntwo")
        assert settings_io.read_seeds(p) == "one\ntwo\n"


class TestApi:
    def test_index_serves(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Opportunity map" in r.text

    def test_empty_db_endpoints_do_not_crash(self, client):
        assert client.get("/api/apps").json() == {"rows": [], "total": 0, "genres": []}
        stats = client.get("/api/stats").json()
        assert stats["counts"]["apps"] == 0
        assert stats["passed"] == 0

    def test_settings_get_and_post(self, client, cfg_file):
        assert client.get("/api/settings").status_code == 200
        r = client.post("/api/settings", json={"filters": {"min_staleness_days": 700}})
        assert r.status_code == 200
        assert load_config(cfg_file).filters.min_staleness_days == 700

    def test_bad_settings_returns_400_not_500(self, client):
        r = client.post("/api/settings", json={"weights": {"build_cost": 0.99}})
        assert r.status_code == 400
        assert "rolled back" in r.json()["detail"]

    def test_unknown_app_404s(self, client):
        assert client.get("/api/app/999").status_code == 404

    def test_unknown_stage_rejected(self, client):
        assert client.post("/api/run/nonsense", json={}).status_code == 400

    def test_score_endpoint_on_empty_db(self, client):
        assert client.post("/api/score").json()["scored"] == 0

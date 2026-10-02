"""Feature 10: GET /gee and POST /gee/{id}/refresh tell the truth about live / cache / fallback.
Earth Engine is replaced by `SyntheticProvider` (or forced unavailable); no network."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.m0_api import schemas
from backend.m0_api.main import app
from backend.m7_gee import fetch as gee_fetch
from backend.m7_gee import imagery as gee_imagery
from backend.m7_gee import scene_search
from backend.m7_gee.imagery import ImageryResult
from backend.m7_gee.provider import SyntheticProvider

API = "/api/v1"
SITE = "teesta"
client = TestClient(app)


def _no_ee(monkeypatch):
    def _never(*a, **k):
        raise RuntimeError("live Earth Engine disabled for this test")
    monkeypatch.setattr(scene_search, "_ee_initialize", _never)


def _fake_ee(monkeypatch, provider=None, imagery_source="cache"):
    monkeypatch.setattr(gee_fetch, "best_effort_provider", lambda *a, **k: provider or SyntheticProvider())
    monkeypatch.setattr(gee_imagery, "refresh", lambda site_id, **k: ImageryResult(
        site_id=site_id, source=imagery_source, errors=[] if imagery_source == "live" else ["imagery: no scene"]))


def _ok(r):
    assert r.status_code == 200, r.text
    schemas.validate("gee_layers.schema.json", r.json())
    return r.json()


def test_get_with_no_cache_is_empty_not_the_mock(data_dir):
    body = _ok(client.get(f"{API}/gee/{SITE}"))
    assert body["data_available"] is False and body["source"] == "cache"
    assert body["lake_area_series"] == [] and body["lake_latest"]["features"] == []
    assert body["fetched_at"] == ""


def test_missing_site_is_404(data_dir):
    assert client.get(f"{API}/gee/nosuchsite").status_code == 404
    assert client.post(f"{API}/gee/nosuchsite/refresh").status_code == 404


def test_refresh_without_credentials_is_cache_and_says_why(data_dir, monkeypatch):
    _no_ee(monkeypatch)
    body = _ok(client.post(f"{API}/gee/{SITE}/refresh"))
    assert body["source"] != "live" and body["partial"] is False
    assert body["refresh"]["earth_engine_initialised"] is False and body["refresh"]["live_products"] == []
    assert body["data_available"] is False
    text = json.dumps(body)
    assert "GEE_SERVICE_ACCOUNT_KEY_PATH=" not in text and "private_key" not in text


def test_live_refresh_then_get(data_dir, monkeypatch):
    _fake_ee(monkeypatch, imagery_source="live")
    body = _ok(client.post(f"{API}/gee/{SITE}/refresh"))
    assert body["source"] == "live" and body["data_available"] is True
    assert set(body["refresh"]["live_products"]) == {"lake_area", "lake_latest", "rainfall", "imagery"}
    assert body["partial"] is False
    assert body["products"]["lake_area"]["source"] == "live"

    got = _ok(client.get(f"{API}/gee/{SITE}"))
    assert got["source"] == "cache"  # GET never claims live
    assert got["fetched_at"] == body["fetched_at"] and got["last_refresh"]["live_products"]


def test_partial_refresh_is_flagged(data_dir, monkeypatch):
    _fake_ee(monkeypatch, imagery_source="cache")
    body = _ok(client.post(f"{API}/gee/{SITE}/refresh"))
    assert body["source"] == "live" and body["partial"] is True
    assert body["refresh"]["products"]["imagery"] == "cache"
    assert any("imagery" in e for e in body["refresh"]["errors"])


def test_refresh_failure_after_live_keeps_cache(data_dir, monkeypatch):
    real_provider, real_imagery = gee_fetch.best_effort_provider, gee_imagery.refresh
    _fake_ee(monkeypatch, imagery_source="live")
    first = _ok(client.post(f"{API}/gee/{SITE}/refresh"))
    monkeypatch.setattr(gee_fetch, "best_effort_provider", real_provider)
    monkeypatch.setattr(gee_imagery, "refresh", real_imagery)
    _no_ee(monkeypatch)
    second = _ok(client.post(f"{API}/gee/{SITE}/refresh"))
    assert second["source"] == "cache" and second["refresh"]["live_products"] == []
    assert second["lake_area_series"] == first["lake_area_series"]
    assert second["fetched_at"] == first["fetched_at"]
    assert second["products"]["lake_area"]["source"] == "cache"
    assert second["products"]["lake_area"]["fallback_reason"] == "provider_error"

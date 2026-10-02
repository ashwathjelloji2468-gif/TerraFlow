"""Feature 10: near-real-time monitoring semantics -- truthful live/cache/fallback, independent
product failures, per-product provenance, staged imagery swaps, sanitised errors. Everything runs
on `SyntheticProvider` / monkeypatched renders: no Earth Engine, no network."""
from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio

from backend.m0_api import schemas
from backend.m7_gee import cache, fetch, imagery
from backend.m7_gee.provider import SyntheticProvider
from backend.m7_gee.settings import GeeSettings
from backend.shared.site_config import load_site_config
from tests.m7_gee.test_imagery import cfg_and_root  # noqa: F401

FIXTURES_DIR = "tests/fixtures/shared"
S = GeeSettings(months_back=4)


@pytest.fixture(autouse=True)
def _synth_site(monkeypatch):
    monkeypatch.setattr(fetch, "load_site_config", lambda site_id: load_site_config("synth", sites_dir=FIXTURES_DIR))


class _RainDown(SyntheticProvider):
    def catchment(self, *a, **k):
        raise RuntimeError("rainfall service down at /home/user/.config/earthengine/credentials")


class _OneMonthDown(SyntheticProvider):
    def __init__(self, bad_month: str, **kw):
        super().__init__(**kw)
        self.bad_month = bad_month

    def s2_month(self, grid, month_start, month_end):
        if month_start.strftime("%Y-%m") == self.bad_month:
            raise RuntimeError("computePixels quota exceeded")
        return super().s2_month(grid, month_start, month_end)


class _AllDown(SyntheticProvider):
    def s2_month(self, *a, **k):
        raise RuntimeError("Earth Engine unavailable")

    s1_month = catchment = rainfall_daily = s2_month


def _meta(tmp_path):
    return json.loads((cache.gee_dir("synth", tmp_path) / "gee_meta.json").read_text())


def _layers(tmp_path):
    layers = cache.load_layers("synth", tmp_path)
    schemas.validate("gee_layers.schema.json", layers)
    return layers


def test_get_after_live_fetch_is_cache_with_live_product_provenance(tmp_path):
    result = fetch.run("synth", settings=S, provider=SyntheticProvider(), data_dir=tmp_path)
    assert result.products == {"lake_area": "live", "lake_latest": "live", "rainfall": "live"}
    layers = _layers(tmp_path)
    assert layers["source"] == "cache"  # GET serves the on-disk cache; only a refresh says live
    assert layers["data_available"] is True and layers["partial"] is False
    p = layers["products"]
    assert p["lake_area"]["source"] == "live" and p["lake_area"]["fetched_at"] == layers["fetched_at"]
    assert p["lake_latest"]["acquisition_dates"] and p["lake_latest"]["method"] == "s2_water_index"
    assert p["rainfall"]["dataset"] == "chirps" and "not gauge" in p["rainfall"]["method"]
    assert layers["rainfall_kind"] == "satellite_estimate"
    assert set(layers["rainfall_accumulations"]) >= {"7d_mm", "30d_mm"}
    assert layers["recheck"]["threshold_pct"] == S.recheck_threshold_pct
    assert layers["recheck"]["reason"] == "no_trained_library"
    props = layers["lake_latest"]["features"][0]["properties"]
    assert props["kind"] == "observed" and props["acquisition_dates"]


def test_rainfall_failure_is_independent_and_reported(tmp_path):
    result = fetch.run("synth", settings=S, provider=_RainDown(), data_dir=tmp_path)
    assert result.products["lake_area"] == "live" and result.products["rainfall"] == "cache"
    layers = _layers(tmp_path)
    assert layers["lake_area_series"] and layers["rainfall"] == []
    assert layers["partial"] is True
    rain = layers["products"]["rainfall"]
    assert rain["source"] == "none" and rain["fetched_at"] is None  # never fetched: not "cache"
    assert rain["fallback_reason"] == "provider_error"
    assert "<path>" in rain["error"] and "/home/user" not in rain["error"]


def test_one_failed_month_keeps_the_other_live_months(tmp_path):
    months = [m.strftime("%Y-%m") for m in fetch._month_starts(S.months_back)]
    result = fetch.run("synth", settings=S, provider=_OneMonthDown(months[1]), data_dir=tmp_path)
    assert result.products["lake_area"] == "live"
    rows = cache.read_lake_area("synth", tmp_path)
    assert {r["date"][:7] for r in rows} == set(months) - {months[1]}
    meta = _meta(tmp_path)
    assert meta["lake_area_months"][f"{months[1]}-01"]["method"] == "error"
    assert "1 month(s) failed" in meta["lake_area"]["error"]


def test_total_outage_keeps_cache_and_previous_fetched_at(tmp_path):
    fetch.run("synth", settings=S, provider=SyntheticProvider(), data_dir=tmp_path)
    first = _meta(tmp_path)
    rows = cache.read_lake_area("synth", tmp_path)
    rain = cache.read_rainfall("synth", tmp_path)
    result = fetch.run("synth", settings=S, provider=_AllDown(), data_dir=tmp_path)
    assert not result.any_live and result.errors
    meta = _meta(tmp_path)
    assert meta["fetched_at"] == first["fetched_at"]  # not re-stamped by a cache-only run
    for name in ("lake_area", "rainfall"):
        assert meta[name]["source"] == "cache" and meta[name]["fetched_at"] == first[name]["fetched_at"]
        assert meta[name]["last_attempt_at"] >= first[name]["fetched_at"]
    assert cache.read_lake_area("synth", tmp_path) == rows and cache.read_rainfall("synth", tmp_path) == rain


def test_rainfall_cache_is_merged_not_replaced(tmp_path):
    cache.write_rainfall("synth", [{"date": "2001-01-01", "precip_mm": 4.0, "dataset": "chirps",
                                    "aggregation": "catchment_mean_daily_total"}], tmp_path)
    fetch.run("synth", settings=S, provider=SyntheticProvider(), data_dir=tmp_path)
    dates = [r["date"] for r in cache.read_rainfall("synth", tmp_path)]
    assert dates[0] == "2001-01-01" and len(dates) == S.rain_days_back + 1


def test_fetch_keeps_imagery_provenance(tmp_path):
    cache.write_json("synth", "gee_meta.json", {"imagery": {"source": "cache", "fetched_at": "x"}}, tmp_path)
    fetch.run("synth", settings=S, provider=SyntheticProvider(), data_dir=tmp_path)
    assert _meta(tmp_path)["imagery"] == {"source": "cache", "fetched_at": "x"}


def test_empty_site_is_honestly_empty(tmp_path):
    layers = _layers(tmp_path)
    assert layers["data_available"] is False and layers["fetched_at"] == ""
    assert layers["lake_area_series"] == [] and layers["lake_latest"]["features"] == []
    assert all(p["source"] is None for p in layers["products"].values())


def test_screenshot_fallback_only_when_no_real_product(tmp_path):
    fb = cache.gee_dir("synth", tmp_path) / "fallback"
    fb.mkdir(parents=True)
    (fb / "lake.png").write_bytes(b"\x89PNG")
    layers = _layers(tmp_path)
    assert layers["source"] == "screenshot_fallback" and layers["fallback_screenshot_count"] == 1
    fetch.run("synth", settings=S, provider=SyntheticProvider(), data_dir=tmp_path)
    assert _layers(tmp_path)["source"] == "cache"


def test_safe_error_redacts_paths_and_secrets():
    msg = cache.safe_error(RuntimeError("bad key /secrets/sa.json private_key=-----BEGIN"))
    assert "/secrets" not in msg and "BEGIN" not in msg and msg.startswith("RuntimeError")


# --- imagery -------------------------------------------------------------------------------------
def _read(path):
    with rasterio.open(path) as ds:
        return ds.read()


def test_live_imagery_swaps_in_only_after_both_phases(cfg_and_root, tmp_path, monkeypatch):
    cfg, repo_root = cfg_and_root
    event = cfg.events[0]
    finals = [imagery.raw_rgb_path(sv.source, repo_root) for sv in (event.imagery_pre_event, event.imagery_post_event)]
    before = [_read(p) for p in finals]

    def half_render(cfg_, event_, repo_root=None, ee_project=None):
        staged = imagery.staging_path(finals[0])
        with rasterio.open(finals[0]) as src:
            profile = src.profile
        with rasterio.open(staged, "w", **profile) as dst:
            dst.write(np.zeros((3, profile["height"], profile["width"]), np.uint8))
        raise RuntimeError("no Sentinel-2 scene near post-event date")

    monkeypatch.setattr("backend.m7_gee.live_render.render_event_rgb", half_render)
    result = imagery.refresh("testsite", cfg=cfg, data_dir=tmp_path / "data", repo_root=repo_root)
    assert result.source == "cache"
    for p, b in zip(finals, before):
        np.testing.assert_array_equal(_read(p), b)  # originals untouched
        assert not imagery.staging_path(p).exists()  # half-finished render cleaned up
    meta = cache.read_json("testsite", "gee_meta.json", tmp_path / "data")
    assert meta["imagery"]["fallback_reason"] == "provider_error" and meta["imagery"]["source"] == "none"


def test_live_imagery_success_records_scene_provenance(cfg_and_root, tmp_path, monkeypatch):
    cfg, repo_root = cfg_and_root
    event = cfg.events[0]

    def full_render(cfg_, event_, repo_root=None, ee_project=None):
        out = {}
        for phase, sv in (("pre", event.imagery_pre_event), ("post", event.imagery_post_event)):
            final = imagery.raw_rgb_path(sv.source, repo_root)
            staged = imagery.staging_path(final)
            with rasterio.open(final) as src:
                profile = src.profile
            with rasterio.open(staged, "w", **profile) as dst:
                dst.write(np.full((3, profile["height"], profile["width"]), 7, np.uint8))
            out[phase] = {"staged": staged, "final": final, "scene_id": f"S2_{phase}",
                          "cloud_pct": 3.0, "acquisition_date": "2020-01-02"}
        return out

    monkeypatch.setattr("backend.m7_gee.live_render.render_event_rgb", full_render)
    result = imagery.refresh("testsite", cfg=cfg, data_dir=tmp_path / "data", repo_root=repo_root)
    assert result.source == "live" and not result.errors
    assert (_read(imagery.raw_rgb_path(event.imagery_pre_event.source, repo_root)) == 7).all()
    meta = cache.read_json("testsite", "gee_meta.json", tmp_path / "data")["imagery"]
    assert meta["source"] == "live" and meta["scene_ids"] == ["S2_post", "S2_pre"] and meta["cloud_pct"] == 3.0


def test_imagery_with_nothing_staged_reports_none(tmp_path, monkeypatch):
    from tests.m7_gee.test_imagery import _Cfg, _Event, _SourcedValue
    cfg = _Cfg(events=[_Event("e", _SourcedValue("2020-01-01", "cache/gee/x/a.tif"),
                              _SourcedValue("2020-01-10", "cache/gee/x/b.tif"))])
    monkeypatch.setattr("backend.m7_gee.live_render.render_event_rgb",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")))
    result = imagery.refresh("testsite", cfg=cfg, data_dir=tmp_path / "data", repo_root=tmp_path / "repo")
    assert result.source == "none" and result.manifest_path is None
    assert cache.read_imagery("testsite", tmp_path / "data") == []

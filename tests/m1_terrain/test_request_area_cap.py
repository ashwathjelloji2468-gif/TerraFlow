"""Feature 16 P1: the far-field DEM/landcover download request is size-capped from
`config/m1_ingestion.yaml` (`max_request_area_km2`) and refused BEFORE any download. Synthetic
configs only; the network is never touched."""
from __future__ import annotations

import math
import warnings

import pytest
import yaml

from backend.m1_terrain import download, ingest
from backend.shared.site_config import PlaceholderWarning, SiteConfig, load_site_config


def _load(site_id: str) -> SiteConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        return load_site_config(site_id)


def _with_bbox(cfg: SiteConfig, bbox: list[float]) -> SiteConfig:
    raw = cfg.model_dump(by_alias=True)
    raw["domains"]["far_field"]["bbox"]["value"] = bbox
    return SiteConfig.model_validate(raw)


def test_area_formula_matches_spherical_band():
    # 1 deg x 1 deg at the equator on R = 6371.0088 km
    r = download.EARTH_RADIUS_KM
    assert download.bbox_area_km2((0.0, 0.0, 1.0, 1.0)) == pytest.approx(r * r * math.radians(1) * math.sin(math.radians(1)))
    assert download.bbox_area_km2((88.0, 27.0, 89.0, 28.0)) < download.bbox_area_km2((88.0, 0.0, 89.0, 1.0))


def test_setting_default_and_config_file_agree():
    assert ingest.IngestionSettings().max_request_area_km2 == 25000.0
    raw = yaml.safe_load(ingest.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert raw["max_request_area_km2"] == 25000
    assert ingest.load_settings().max_request_area_km2 == 25000.0


@pytest.mark.parametrize("bad", [0, -1, "big"])
def test_setting_must_be_positive_number(tmp_path, bad):
    path = tmp_path / "m1.yaml"
    path.write_text(yaml.safe_dump({"max_request_area_km2": bad}))
    with pytest.raises(Exception):
        ingest.load_settings(path)


@pytest.mark.parametrize("site_id", ["teesta", "demo_valley", "synth_engdam"])
def test_existing_configs_stay_under_the_default_cap(site_id):
    area = download.check_request_area(_load(site_id), ingest.IngestionSettings().max_request_area_km2)
    assert 0 < area < 25000


def test_check_is_deterministic_and_includes_margin():
    cfg = _load("demo_valley")
    a = download.check_request_area(cfg, 1e9)
    assert a == download.check_request_area(cfg, 1e9)
    assert a == round(download.bbox_area_km2(download.site_bbox_with_margin(cfg)), 1)
    assert a > download.bbox_area_km2(tuple(cfg.domains.far_field.bbox.value))


def test_oversized_request_raises_with_numbers():
    cfg = _with_bbox(_load("demo_valley"), [80.0, 25.0, 90.0, 32.0])
    with pytest.raises(download.RequestAreaTooLarge) as exc:
        download.check_request_area(cfg, 25000)
    assert exc.value.area_km2 > 25000 and exc.value.max_area_km2 == 25000
    assert "max_request_area_km2" in str(exc.value)


def test_cap_is_inclusive():
    cfg = _load("demo_valley")
    area = download.check_request_area(cfg, 1e9)
    assert download.check_request_area(cfg, area) == area
    with pytest.raises(download.RequestAreaTooLarge):
        download.check_request_area(cfg, area - 0.1)


def test_ingest_refuses_before_any_download(tmp_path, monkeypatch):
    cfg = _load("demo_valley")
    calls = []
    monkeypatch.setattr(download, "fetch_opentopography", lambda *a, **k: calls.append("dem"))
    monkeypatch.setattr(download, "fetch_worldcover", lambda *a, **k: calls.append("wc"))
    monkeypatch.setenv(ingest.NETWORK_ENV, "on")
    settings = ingest.IngestionSettings(max_request_area_km2=1.0)
    raw = tmp_path / "raw"
    with pytest.raises(ingest.IngestionError) as exc:
        ingest.ingest_site(cfg, raw, settings=settings)
    assert exc.value.job_error_code == "terrain_failed"
    d = exc.value.job_error_details
    assert d["reason"] == "request_area_too_large" and d["max_request_area_km2"] == 1.0
    assert d["request_area_km2"] > 1.0 and d["site_id"] == "demo_valley"
    assert calls == [] and [p.name for p in raw.iterdir()] == []


def test_download_cli_refuses_oversized_request(monkeypatch, tmp_path):
    cfg = _with_bbox(_load("demo_valley"), [80.0, 25.0, 90.0, 32.0])
    monkeypatch.setattr(download, "load_site_config", lambda _id: cfg)
    monkeypatch.setattr(download, "fetch_opentopography", lambda *a, **k: pytest.fail("downloaded"))
    monkeypatch.setattr(download, "fetch_worldcover", lambda *a, **k: pytest.fail("downloaded"))
    with pytest.raises(SystemExit, match="max_request_area_km2"):
        download.main(["demo_valley", "--data-dir", str(tmp_path)])

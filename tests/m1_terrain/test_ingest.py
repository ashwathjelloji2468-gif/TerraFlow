"""Feature 2: backend/m1_terrain/ingest.py, hydrobasins.py, discharge.py -- on the synthetic valley,
with every external service replaced by an in-process fake (httpx.MockTransport, local tiles, a
tiny local HydroBASINS shapefile). No test reaches a real service."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import httpx
import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from backend.m1_terrain import discharge, download, hydrobasins, ingest
from backend.shared.site_config import SiteConfig

from . import synthetic_valley as sv

FAKE_KEY = "sekrit-ingest-key-should-never-leak"


# --- fixtures -----------------------------------------------------------------------------------
@pytest.fixture
def cfg(synth_config) -> SiteConfig:
    return synth_config


@pytest.fixture
def valley_files(tmp_path, cfg) -> dict[str, bytes]:
    """Bytes of a synthetic DEM and a matching WorldCover tile store."""
    src = tmp_path / "valley_src"
    sv.write_raw_rasters(src, cfg.domains.far_field.bbox.value, tuple(cfg.dams[0].breach_location.value),
                         tuple(cfg.points_of_interest[0].location.value), tuple(cfg.dams[0].location.value))
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    # a 3x3-degree WorldCover-style tile (EPSG:4326, uint8, nodata 0) covering the synth bbox
    profile = {"driver": "GTiff", "width": 300, "height": 300, "count": 1, "dtype": "uint8", "nodata": 0,
               "crs": "EPSG:4326", "transform": rasterio.transform.from_bounds(87, 27, 90, 30, 300, 300)}
    with rasterio.open(tiles / "ESA_WorldCover_10m_2021_v200_N27E087_Map.tif", "w", **profile) as ds:
        ds.write(np.full((1, 300, 300), 30, dtype=np.uint8))
    return {"dem": (src / "dem_srtm_gl1.tif").read_bytes(), "tiles": str(tiles)}


def _settings(tiles: str, **over) -> ingest.IngestionSettings:
    base = ingest.load_settings().model_dump()
    base.update(worldcover_base_url=tiles, **over)
    return ingest.IngestionSettings.model_validate(base)


def _ot_client(dem_bytes: dict[str, bytes] | bytes, calls: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        demtype = request.url.params["demtype"]
        if calls is not None:
            calls.append(demtype)
        body = dem_bytes[demtype] if isinstance(dem_bytes, dict) else dem_bytes
        return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _discharge_client(status=200, values=(12.0, 15.5, None, 20.0)) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="upstream error")
        times = [f"2024-01-0{i + 1}" for i in range(len(values))]
        return httpx.Response(200, json={"latitude": float(request.url.params["latitude"]),
                                         "longitude": float(request.url.params["longitude"]),
                                         "daily_units": {"time": "iso8601", "river_discharge": "m³/s"},
                                         "daily": {"time": times, "river_discharge": list(values)}})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def network_on(monkeypatch):
    monkeypatch.setenv("SIH26_INGEST_NETWORK", "on")
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", FAKE_KEY)
    monkeypatch.delenv("HYDROBASINS_DIR", raising=False)
    monkeypatch.delenv("GEE_PROJECT", raising=False)
    monkeypatch.setattr(hydrobasins, "_earth_engine_configured", lambda: False)


def _with_voids(dem: bytes, tmp_path: Path, fraction: float) -> bytes:
    src = tmp_path / "void_src.tif"
    src.write_bytes(dem)
    with rasterio.open(src) as ds:
        arr, profile = ds.read(1), ds.profile
    rows = int(arr.shape[0] * fraction)
    arr[:rows, :] = -9999.0
    profile.update(nodata=-9999.0)
    out = tmp_path / f"void_{fraction}.tif"
    with rasterio.open(out, "w", **profile) as ds:
        ds.write(arr, 1)
    return out.read_bytes()


# --- 1. readiness ---------------------------------------------------------------------------------
def test_readiness_lists_every_missing_field(synth_raw):
    raw = copy.deepcopy(synth_raw)
    raw["domains"]["near_field"]["bbox"] = {"value": None, "unit": "deg", "source": "", "status": "placeholder"}
    raw["domains"]["far_field"]["grid_resolution"] = {"value": None, "unit": "m", "source": "", "status": "placeholder"}
    raw["dams"][0]["breach_location"] = {"value": None, "unit": "deg", "source": "", "status": "placeholder"}
    cfg = SiteConfig.model_validate(raw)
    with pytest.raises(ingest.IngestionError) as e:
        ingest.check_terrain_readiness(cfg)
    assert e.value.job_error_code == "terrain_inputs_incomplete"
    assert e.value.job_error_details["missing_fields"] == [
        "domains.far_field.grid_resolution", "domains.near_field.bbox", f"dams[0] ({cfg.dams[0].id}).breach_location"]


def test_incomplete_config_downloads_nothing(tmp_path, synth_raw, network_on):
    raw = copy.deepcopy(synth_raw)
    raw["crs"]["utm_epsg"] = {"value": None, "unit": "epsg", "source": "", "status": "placeholder"}
    with pytest.raises(ingest.IngestionError, match="crs.utm_epsg"):
        ingest.ingest_site(SiteConfig.model_validate(raw), tmp_path / "raw",
                           client=_ot_client(b"never"), settings=_settings(str(tmp_path)))
    assert list((tmp_path / "raw").iterdir()) == []


# --- 2. full ingestion, DEM comparison and selection ------------------------------------------------
def test_full_ingestion_prefers_copernicus_and_records_everything(tmp_path, cfg, valley_files, network_on):
    calls: list[str] = []
    raw_dir = tmp_path / "raw"
    summary = ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                                 client=_ot_client(valley_files["dem"], calls),
                                 discharge_client=_discharge_client())
    assert sorted(calls) == ["COP30", "SRTMGL1"]  # D2: both candidates downloaded
    assert summary["selected_dem"] == "copernicus_glo30"  # D1
    comparison = json.loads((raw_dir / "dem_comparison.json").read_text())
    assert comparison["selected"] == "copernicus_glo30" and comparison["threshold_met"] is True
    assert {c["product"] for c in comparison["candidates"]} == {"copernicus_glo30", "srtm_gl1"}
    srtm = next(c for c in comparison["candidates"] if c["product"] == "srtm_gl1")
    assert srtm["difference_vs_selected_m"]["mean_abs"] == pytest.approx(0.0, abs=1e-3)
    prov = json.loads((raw_dir / "provenance.json").read_text())
    for key in ("dem_copernicus_glo30", "dem_srtm_gl1", "landcover_esa_worldcover"):
        assert prov[key]["status"] == "fetched" and prov[key]["sha256"] and prov[key]["request_bbox_deg"]
    assert prov["dem_copernicus_glo30"]["vertical_datum"] == "EGM2008"
    assert prov["hydrobasins_catchment"]["status"] == "unavailable"
    assert "HYDROBASINS_DIR is not set" in prov["hydrobasins_catchment"]["reason"]
    assert prov["discharge_glofas"]["status"] == "fetched"
    assert FAKE_KEY not in (raw_dir / "provenance.json").read_text()
    assert FAKE_KEY not in (raw_dir / "dem_comparison.json").read_text()


def test_void_threshold_falls_back_to_srtm(tmp_path, cfg, valley_files, network_on):
    holey = _with_voids(valley_files["dem"], tmp_path, 0.5)
    summary = ingest.ingest_site(cfg, tmp_path / "raw", settings=_settings(valley_files["tiles"]),
                                 client=_ot_client({"COP30": holey, "SRTMGL1": valley_files["dem"]}),
                                 discharge_client=_discharge_client())
    assert summary["selected_dem"] == "srtm_gl1"
    cop = next(c for c in summary["dem_comparison"]["candidates"] if c["product"] == "copernicus_glo30")
    assert cop["void_fraction"] > 0.05


def test_no_candidate_within_threshold_picks_lowest_void_and_says_so(tmp_path, cfg, valley_files, network_on):
    a, b = _with_voids(valley_files["dem"], tmp_path, 0.6), _with_voids(valley_files["dem"], tmp_path, 0.3)
    summary = ingest.ingest_site(cfg, tmp_path / "raw", settings=_settings(valley_files["tiles"]),
                                 client=_ot_client({"COP30": a, "SRTMGL1": b}), discharge_client=_discharge_client())
    assert summary["selected_dem"] == "srtm_gl1"
    assert summary["dem_comparison"]["threshold_met"] is False


# --- 3. credentials -----------------------------------------------------------------------------------
def test_missing_api_key_is_actionable_and_blocks(tmp_path, cfg, valley_files, network_on, monkeypatch):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY")
    monkeypatch.setattr(download, "ENV_FILE", tmp_path / "no.env")
    with pytest.raises(ingest.IngestionError) as e:
        ingest.ingest_site(cfg, tmp_path / "raw", settings=_settings(valley_files["tiles"]),
                           client=_ot_client(b"unused"), discharge_client=_discharge_client())
    assert e.value.job_error_code == "terrain_failed"
    assert "OPENTOPOGRAPHY_API_KEY is not set" in str(e.value)
    assert "opentopography.org" in str(e.value)
    assert not list((tmp_path / "raw").glob("dem_*.tif"))  # nothing fabricated


# --- 4. cache validation -------------------------------------------------------------------------------
def test_cached_dem_covering_the_area_is_reused_without_download(tmp_path, cfg, valley_files, network_on):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "dem_copernicus_glo30.tif").write_bytes(valley_files["dem"])
    calls: list[str] = []
    ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                       client=_ot_client(valley_files["dem"], calls), discharge_client=_discharge_client())
    assert calls == ["SRTMGL1"]


def test_cached_dem_for_a_different_area_is_rejected_and_refetched(tmp_path, cfg, valley_files, network_on):
    raw_dir = tmp_path / "raw"
    other = tmp_path / "other"
    sv.write_raw_rasters(other, (88.50, 27.50, 88.55, 27.55), (88.51, 27.54), (88.52, 27.51), (88.51, 27.545))
    raw_dir.mkdir()
    (raw_dir / "dem_copernicus_glo30.tif").write_bytes((other / "dem_srtm_gl1.tif").read_bytes())
    problem = ingest.validate_cached_raster(raw_dir / "dem_copernicus_glo30.tif", cfg.domains.far_field.bbox.value, None)
    assert problem and "site requests" in problem
    calls: list[str] = []
    ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                       client=_ot_client(valley_files["dem"], calls), discharge_client=_discharge_client())
    assert sorted(calls) == ["COP30", "SRTMGL1"]


def test_cached_file_checksum_mismatch_is_rejected(tmp_path, cfg, valley_files):
    path = tmp_path / "dem.tif"
    path.write_bytes(valley_files["dem"])
    assert "checksum" in ingest.validate_cached_raster(path, cfg.domains.far_field.bbox.value, {"sha256": "0" * 64})


def test_offline_with_wrong_cache_is_unavailable_not_reused(tmp_path, cfg, valley_files, monkeypatch):
    monkeypatch.setenv("SIH26_INGEST_NETWORK", "off")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "dem_copernicus_glo30.tif").write_text("not a raster")
    # valid cached land cover, so the DEM problem is what surfaces
    (raw_dir / "landcover_esa_worldcover.tif").write_bytes(
        (Path(valley_files["tiles"]) / "ESA_WorldCover_10m_2021_v200_N27E087_Map.tif").read_bytes())
    with pytest.raises(ingest.IngestionError, match="no DEM candidate is available"):
        ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]))


# --- 5. WorldCover failures ------------------------------------------------------------------------------
def test_worldcover_network_failure_is_an_explicit_error(tmp_path, cfg):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(download.DownloadError, match="HTTP 503"):
        download.fetch_worldcover(cfg, tmp_path / "raw", base_url="https://example.invalid/map", client=client)
    assert not (tmp_path / "raw" / "landcover_esa_worldcover.tif").exists()


def test_worldcover_connection_error_is_an_explicit_error(tmp_path, cfg):
    def boom(request):
        raise httpx.ConnectError("refused")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(download.DownloadError, match="could not be checked"):
        download.fetch_worldcover(cfg, tmp_path / "raw", base_url="https://example.invalid/map", client=client)


def test_worldcover_absent_tiles_only_is_no_land_error(tmp_path, cfg):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(download.DownloadError, match="no ESA WorldCover tiles"):
        download.fetch_worldcover(cfg, tmp_path / "raw", base_url="https://example.invalid/map", client=client)


def test_landcover_coverage_gap_fails(tmp_path, cfg, valley_files, network_on):
    tiles = Path(valley_files["tiles"])
    tile = tiles / "ESA_WorldCover_10m_2021_v200_N27E087_Map.tif"
    with rasterio.open(tile, "r+") as ds:
        arr = ds.read(1)
        arr[-60:, :] = 0  # blank 27.0-27.6 N, which contains the whole synth site
        ds.write(arr, 1)
    with pytest.raises(ingest.IngestionError, match="covers only"):
        ingest.ingest_site(cfg, tmp_path / "raw", settings=_settings(str(tiles)),
                           client=_ot_client(valley_files["dem"]), discharge_client=_discharge_client())


# --- 6. CartoDEM only when supplied ----------------------------------------------------------------------
def test_cartodem_is_a_candidate_only_when_supplied(tmp_path, cfg, valley_files, network_on):
    raw_dir = tmp_path / "raw"
    summary = ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                                 client=_ot_client(valley_files["dem"]), discharge_client=_discharge_client())
    assert "cartodem" not in {c["product"] for c in summary["dem_comparison"]["candidates"]}
    (raw_dir / "dem_cartodem.tif").write_bytes(valley_files["dem"])
    download._merge_provenance(raw_dir, "dem_cartodem", {"file": "dem_cartodem.tif", "status": "fetched"})
    summary = ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                                 client=_ot_client(valley_files["dem"]), discharge_client=_discharge_client())
    assert "cartodem" in {c["product"] for c in summary["dem_comparison"]["candidates"]}
    assert summary["selected_dem"] == "copernicus_glo30"  # still preferred


# --- 7. HydroBASINS ----------------------------------------------------------------------------------------
def _write_hydrobasins(root: Path, lon: float, lat: float) -> Path:
    """Three chained sub-basins: 3 -> 2 -> 1 (outlet), the dam inside basin 2, plus an unrelated 9."""
    root.mkdir(parents=True, exist_ok=True)
    d = 0.02
    gdf = gpd.GeoDataFrame(
        {"HYBAS_ID": [1, 2, 3, 9], "NEXT_DOWN": [0, 1, 2, 0], "SUB_AREA": [10.0, 20.0, 30.0, 5.0], "UP_AREA": [60.0, 50.0, 30.0, 5.0]},
        geometry=[box(lon - d, lat - 3 * d, lon + d, lat - d), box(lon - d, lat - d, lon + d, lat + d),
                  box(lon - d, lat + d, lon + d, lat + 3 * d), box(lon + 5 * d, lat, lon + 6 * d, lat + d)],
        crs=4326)
    path = root / "hybas_as_lev12_v1c.shp"
    gdf.to_file(path)
    return path


def test_hydrobasins_local_shapefile_walks_upstream(tmp_path, cfg, monkeypatch):
    lon, lat = cfg.dams[0].location.value
    _write_hydrobasins(tmp_path / "hb", lon, lat)
    monkeypatch.setenv("HYDROBASINS_DIR", str(tmp_path / "hb"))
    entry = hydrobasins.fetch_catchment(cfg, tmp_path / "raw", level=12, network=False)
    assert entry["status"] == "fetched" and entry["method"] == "local_shapefile"
    assert entry["seed_basin_id"] == 2 and entry["n_basins"] == 2
    assert entry["upstream_area_m2"] == pytest.approx(50.0e6)
    fc = json.loads((tmp_path / "raw" / "hydrobasins_catchment.geojson").read_text())
    assert sorted(f["properties"]["HYBAS_ID"] for f in fc["features"]) == [2, 3]


def test_hydrobasins_unavailable_is_recorded_never_fetched(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("HYDROBASINS_DIR", raising=False)
    monkeypatch.setattr(hydrobasins, "_earth_engine_configured", lambda: False)
    entry = hydrobasins.fetch_catchment(cfg, tmp_path / "raw", network=True)
    assert entry["status"] == "unavailable"
    assert "HYDROBASINS_DIR is not set" in entry["reason"] and "Earth Engine credentials" in entry["reason"]
    assert not (tmp_path / "raw" / "hydrobasins_catchment.geojson").exists()


def test_hydrobasins_point_outside_region_is_unavailable(tmp_path, cfg, monkeypatch):
    _write_hydrobasins(tmp_path / "hb", 10.0, 10.0)
    monkeypatch.setenv("HYDROBASINS_DIR", str(tmp_path / "hb"))
    entry = hydrobasins.fetch_catchment(cfg, tmp_path / "raw", network=False)
    assert entry["status"] == "unavailable" and "wrong HydroBASINS region" in entry["reason"]


def test_required_hydrobasins_blocks_when_unavailable(tmp_path, cfg, valley_files, network_on):
    settings = _settings(valley_files["tiles"], hydrobasins={"level": 12, "required": True})
    with pytest.raises(ingest.IngestionError, match="HydroBASINS catchment is required"):
        ingest.ingest_site(cfg, tmp_path / "raw", settings=settings, client=_ot_client(valley_files["dem"]),
                           discharge_client=_discharge_client())


# --- 8. discharge ---------------------------------------------------------------------------------------------
def test_discharge_series_written_with_units_and_stats(tmp_path, cfg):
    entry = discharge.fetch_discharge(cfg, tmp_path, client=_discharge_client(), end_date=date(2024, 1, 4))
    assert entry["status"] == "fetched" and entry["units"] == "m^3/s"
    assert entry["n_days"] == 4 and entry["n_missing"] == 1
    assert entry["summary_m3s"]["max"] == 20.0 and entry["summary_m3s"]["median"] == 15.5
    assert entry["point_basis"] == "domains.far_field.inflow.location"
    assert entry["kind"].startswith("modelled")
    lines = (tmp_path / "discharge_glofas.csv").read_text().splitlines()
    assert lines[0] == "date,river_discharge_m3s" and lines[3] == "2024-01-03,"


def test_discharge_falls_back_to_dam_location_for_the_point(tmp_path, synth_raw):
    raw = copy.deepcopy(synth_raw)
    raw["domains"]["far_field"]["inflow"]["location"] = {"value": None, "unit": "deg", "source": "", "status": "placeholder"}
    entry = discharge.fetch_discharge(SiteConfig.model_validate(raw), tmp_path, client=_discharge_client())
    assert entry["point_basis"].startswith("dams[0]")


@pytest.mark.parametrize("client, match", [
    (lambda: _discharge_client(status=500), "HTTP 500"),
    (lambda: _discharge_client(values=(None, None)), "no discharge values"),
])
def test_discharge_failures_raise_and_write_nothing(tmp_path, cfg, client, match):
    with pytest.raises(discharge.DischargeUnavailable, match=match):
        discharge.fetch_discharge(cfg, tmp_path, client=client())
    assert not (tmp_path / "discharge_glofas.csv").exists()


def test_discharge_unavailable_is_recorded_in_provenance(tmp_path, cfg, valley_files, network_on):
    raw_dir = tmp_path / "raw"
    ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]),
                       client=_ot_client(valley_files["dem"]), discharge_client=_discharge_client(status=502))
    entry = json.loads((raw_dir / "provenance.json").read_text())["discharge_glofas"]
    assert entry["status"] == "unavailable" and "HTTP 502" in entry["reason"]


# --- 9. offline: nothing fabricated ---------------------------------------------------------------------------
def test_offline_records_unavailable_and_writes_no_data(tmp_path, cfg, valley_files, monkeypatch):
    monkeypatch.setenv("SIH26_INGEST_NETWORK", "off")
    raw_dir = tmp_path / "raw"
    with pytest.raises(ingest.IngestionError):
        ingest.ingest_site(cfg, raw_dir, settings=_settings(valley_files["tiles"]))
    assert sorted(p.name for p in raw_dir.iterdir()) == ["provenance.json"]
    prov = json.loads((raw_dir / "provenance.json").read_text())
    assert all(v["status"] == "unavailable" for v in prov.values())


# --- 10. data dir ------------------------------------------------------------------------------------------------
def test_download_cli_respects_sih26_data_dir(tmp_path, synth_raw, monkeypatch):
    import yaml

    data = tmp_path / "data"
    raw = copy.deepcopy(synth_raw)
    raw["site"]["id"] = "onboarded_cli"
    (data / "onboarded_cli" / "config").mkdir(parents=True)
    (data / "onboarded_cli" / "config" / "onboarded_cli.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("SIH26_DATA_DIR", str(data))
    seen = []
    monkeypatch.setattr(download, "fetch_worldcover", lambda cfg, raw_dir, **k: seen.append(raw_dir) or {"status": "fetched"})
    assert download.main(["onboarded_cli", "--products", "worldcover"]) == 0
    assert seen == [data / "onboarded_cli" / "raw"]


def test_pipeline_cli_respects_sih26_data_dir(tmp_path, synth_raw, monkeypatch):
    import yaml

    from backend.m1_terrain import pipeline

    data = tmp_path / "data"
    raw = copy.deepcopy(synth_raw)
    raw["site"]["id"] = "onboarded_cli"
    (data / "onboarded_cli" / "config").mkdir(parents=True)
    (data / "onboarded_cli" / "config" / "onboarded_cli.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("SIH26_DATA_DIR", str(data))
    seen = []
    monkeypatch.setattr(pipeline, "build_terrain", lambda cfg, dem, raw_dir, out_dir, *a, **k: seen.append((raw_dir, out_dir)))
    assert pipeline.main(["onboarded_cli", "--dem", "srtm_gl1"]) == 0
    assert seen == [(data / "onboarded_cli" / "raw", data / "onboarded_cli" / "terrain")]

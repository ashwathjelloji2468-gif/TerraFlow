"""Feature 2 through the real job system: a newly registered arbitrary site's onboarding job
ingests its own raw data in the `terrain` stage (no CLI steps), selects a DEM and builds M1
terrain. External services are faked in-process; nothing reaches the network."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
import yaml
from fastapi.testclient import TestClient

from backend.m0_api.main import app
from backend.m0_api.worker import Worker
from backend.m1_terrain import discharge, download, hydrobasins, ingest
from tests.m0_api.conftest import wait_until
from tests.m1_terrain import synthetic_valley as sv

REPO = Path(__file__).resolve().parents[2]
API = "/api/v1"
client = TestClient(app)
KEY = "job-test-key-never-logged"


def site_config() -> dict:
    cfg = copy.deepcopy(yaml.safe_load((REPO / "tests/fixtures/shared/synth.yaml").read_text()))
    cfg["site"].update({"id": "ingest_valley", "name": "Ingestion test valley"})
    return cfg


@pytest.fixture
def fake_services(tmp_path, monkeypatch):
    cfg = site_config()
    dam = cfg["dams"][0]
    src = tmp_path / "service"
    sv.write_raw_rasters(src, tuple(cfg["domains"]["far_field"]["bbox"]["value"]), tuple(dam["breach_location"]["value"]),
                         tuple(cfg["points_of_interest"][0]["location"]["value"]), tuple(dam["location"]["value"]))
    dem_bytes = (src / "dem_srtm_gl1.tif").read_bytes()
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    profile = {"driver": "GTiff", "width": 300, "height": 300, "count": 1, "dtype": "uint8", "nodata": 0,
               "crs": "EPSG:4326", "transform": rasterio.transform.from_bounds(87, 27, 90, 30, 300, 300)}
    with rasterio.open(tiles / "ESA_WorldCover_10m_2021_v200_N27E087_Map.tif", "w", **profile) as ds:
        ds.write(np.full((1, 300, 300), 30, dtype=np.uint8))
    settings = yaml.safe_load(ingest.SETTINGS_PATH.read_text())
    settings["worldcover_base_url"] = str(tiles)
    settings_path = tmp_path / "m1_ingestion.yaml"
    settings_path.write_text(yaml.safe_dump(settings))

    monkeypatch.setenv("SIH26_INGEST_NETWORK", "on")
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", KEY)
    monkeypatch.delenv("HYDROBASINS_DIR", raising=False)
    monkeypatch.setattr(hydrobasins, "_earth_engine_configured", lambda: False)
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_path)
    calls: list[str] = []

    def ot_handler(request):
        calls.append(request.url.params["demtype"])
        assert request.url.params["API_Key"] == KEY
        return httpx.Response(200, content=dem_bytes, headers={"content-type": "application/octet-stream"})

    ot_client = httpx.Client(transport=httpx.MockTransport(ot_handler))
    real_ot = download.fetch_opentopography
    monkeypatch.setattr(download, "fetch_opentopography",
                        lambda cfg, product, raw_dir, client=None, force=False: real_ot(cfg, product, raw_dir, client=ot_client, force=force))
    flood = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "latitude": 27.5, "longitude": 88.48, "daily_units": {"river_discharge": "m³/s"},
        "daily": {"time": ["2024-06-01", "2024-06-02"], "river_discharge": [110.0, 130.0]}})))
    real_q = discharge.fetch_discharge
    monkeypatch.setattr(discharge, "fetch_discharge", lambda cfg, raw_dir, **k: real_q(cfg, raw_dir, **{**k, "client": flood}))
    return calls


def _run_until(job_id: str, stages: set[str]) -> dict:
    worker = Worker()
    worker.acquire_lock()
    worker.recover()
    try:
        wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["stage"] in stages, worker.tick, timeout_s=60)
    finally:
        worker.close()
    return client.get(f"{API}/jobs/{job_id}").json()


def test_onboarding_job_ingests_and_builds_terrain_for_an_arbitrary_site(data_dir, fake_services):
    r = client.post(f"{API}/sites", json={"site_config": site_config()})
    assert r.status_code == 202, r.text
    status = _run_until(r.json()["job_id"], {"breach", "design", "simulating", "failed"})
    assert status["stage"] != "failed", status["error"]
    assert sorted(fake_services) == ["COP30", "SRTMGL1"]

    raw = data_dir / "ingest_valley" / "raw"
    prov = json.loads((raw / "provenance.json").read_text())
    assert prov["dem_copernicus_glo30"]["status"] == "fetched"
    assert prov["landcover_esa_worldcover"]["status"] == "fetched"
    assert prov["discharge_glofas"]["status"] == "fetched"
    assert prov["hydrobasins_catchment"]["status"] == "unavailable"
    assert json.loads((raw / "dem_comparison.json").read_text())["selected"] == "copernicus_glo30"
    terrain = json.loads((data_dir / "ingest_valley" / "terrain" / "provenance.json").read_text())
    assert terrain["dem"]["product"] == "copernicus_glo30"
    assert terrain["dem_selection"]["selected"] == "copernicus_glo30"
    assert (data_dir / "ingest_valley" / "terrain" / "dem.tif").is_file()
    log = "\n".join(status["log_tail"])
    assert "dem selected: copernicus_glo30" in log
    for path in raw.iterdir():
        if path.suffix in (".json", ".csv"):
            assert KEY not in path.read_text()


def test_onboarding_job_fails_with_actionable_error_without_api_key(data_dir, fake_services, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY")
    monkeypatch.setattr(download, "ENV_FILE", tmp_path / "missing.env")
    job_id = client.post(f"{API}/sites", json={"site_config": site_config()}).json()["job_id"]
    status = _run_until(job_id, {"failed", "breach"})
    assert status["stage"] == "failed"
    error = status["error"]["error"]
    assert error["code"] == "terrain_failed"
    assert "OPENTOPOGRAPHY_API_KEY is not set" in error["message"]
    assert not list((data_dir / "ingest_valley" / "raw").glob("dem_*.tif"))
    site = client.get(f"{API}/sites/ingest_valley").json()
    assert site["status"] == "failed" and site["status_reason_key"] == "terrain_failed"

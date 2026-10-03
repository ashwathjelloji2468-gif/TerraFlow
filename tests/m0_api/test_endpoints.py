"""Calls every endpoint in docs/handoff_contract.md §5 against the mock M0
server and validates each response against its contracts/schemas/*.json
schema (CLAUDE.md rule 2: every module runs end-to-end on synthetic data
with pytest tests; contract §8: the API test suite calls every endpoint and
validates every response).

`main.py` already validates its own responses before sending them (a 500
there means the server violated its own contract); this file re-validates
independently, the way a real client/CI check would, and additionally checks
status codes, error shapes and the one 404 code path the mock supports.
"""

from __future__ import annotations

import copy
import csv
import importlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.m0_api import jobs, mock_files, registry, schemas, site_status
from backend.m0_api.main import app
from backend.m0_api.worker import Worker
from tests.m0_api.conftest import wait_until

SYNTH_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "shared" / "synth.yaml"


def valid_site_config(site_id: str = "kosi") -> dict:
    """A full, `site_config.schema.json`-valid config for POST /sites tests
    (the schema now `$ref`s the real `SiteConfig` model, contract §5.2)."""
    cfg = copy.deepcopy(yaml.safe_load(SYNTH_FIXTURE.read_text()))
    cfg["site"]["id"] = site_id
    return cfg

client = TestClient(app)

API = "/api/v1"
KNOWN_SITE = "teesta"
NOT_CONFIGURED_SITE = "rishiganga"  # not bundled; an ordinary unconfigured site until onboarded
UNKNOWN_SITE = "nosuchsite"
QUERY_ID = "q_20260924T101500Z_3fa9c1"
JOB_ID = "job_20260924T101500Z_b17e02"
EVENT_ID = "teesta_2023"


def assert_matches(schema_name: str, payload) -> None:
    schemas.validate(schema_name, payload)  # raises ContractViolation on mismatch


# =============================================================================
# 1-2. health, styles
# =============================================================================
def test_health():
    r = client.get(f"{API}/health")
    assert r.status_code == 200
    assert_matches("health.schema.json", r.json())


def test_vite_loopback_origin_is_allowed_for_browser_api_calls():
    response = client.get(f"{API}/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_styles():
    r = client.get(f"{API}/styles")
    assert r.status_code == 200
    body = r.json()
    assert_matches("styles.schema.json", body)
    assert body["contract_version"] == "0.3.0"


# =============================================================================
# 3-4. sites
# =============================================================================
def test_list_sites():
    r = client.get(f"{API}/sites")
    assert r.status_code == 200
    body = r.json()
    assert_matches("site_list.schema.json", body)
    site_ids = {s["site_id"] for s in body}
    assert KNOWN_SITE in site_ids
    # Rishi Ganga is not configured in this MVP; a listed site must not 404 when opened.
    assert NOT_CONFIGURED_SITE not in site_ids
    for site_id in site_ids:
        assert client.get(f"{API}/sites/{site_id}").status_code == 200


def test_get_site_detail_known():
    r = client.get(f"{API}/sites/{KNOWN_SITE}")
    assert r.status_code == 200
    body = r.json()
    assert_matches("site_detail.schema.json", body)
    assert body["site_id"] == KNOWN_SITE


def test_get_site_detail_unconfigured_site_is_generic_site_not_found():
    """Rishi Ganga gets no special case (docs/decisions.md 2026-09-30, decision 3): until someone
    onboards it, it is simply a site that is not configured."""
    r = client.get(f"{API}/sites/{NOT_CONFIGURED_SITE}")
    assert r.status_code == 404
    body = r.json()
    assert_matches("error.schema.json", body["detail"])
    assert body["detail"]["error"]["code"] == "site_not_found"
    assert body["detail"]["error"]["message"] == f"No site '{NOT_CONFIGURED_SITE}' is configured."


def test_get_site_detail_unknown_is_404_with_error_shape():
    r = client.get(f"{API}/sites/{UNKNOWN_SITE}")
    assert r.status_code == 404
    assert_matches("error.schema.json", r.json()["detail"])
    assert r.json()["detail"]["error"]["code"] == "site_not_found"


def test_get_site_detail_malformed_id_is_422():
    r = client.get(f"{API}/sites/NOT-VALID!!")
    assert r.status_code == 422


# =============================================================================
# 5. POST /sites
# =============================================================================
def test_create_site_accepted():
    r = client.post(f"{API}/sites", json={"site_config": valid_site_config(), "demo_mode": True})
    assert r.status_code == 202
    body = r.json()
    assert_matches("site_create_accepted.schema.json", body)
    assert body["site_id"] == "kosi"


def test_create_site_missing_config_is_422():
    r = client.post(f"{API}/sites", json={"demo_mode": True})
    assert r.status_code == 422
    assert_matches("error.schema.json", r.json()["detail"])


@pytest.mark.parametrize("site_config", [{}, {"site_id": "Bad-Id"}, {"site_id": 7}])
def test_create_site_bad_site_id_is_422(site_config):
    r = client.post(f"{API}/sites", json={"site_config": site_config})
    assert r.status_code == 422
    assert_matches("error.schema.json", r.json()["detail"])


def test_create_site_twice_is_409_site_already_exists():
    """Feature 1: a site id is persistent and unique, so a second POST of the same id is rejected
    as a duplicate (not merely while a job is active). The active-job 409 is still covered by
    `test_rerun_site_conflicts_with_active_job`."""
    first = client.post(f"{API}/sites", json={"site_config": valid_site_config()})
    assert first.status_code == 202
    r = client.post(f"{API}/sites", json={"site_config": valid_site_config()})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert_matches("error.schema.json", detail)
    assert detail["error"]["code"] == "site_already_exists"
    assert detail["error"]["details"]["site_id"] == "kosi"


# =============================================================================
# 6. GET /jobs/{job_id}
# =============================================================================
def test_get_job_created_by_post_sites():
    job_id = client.post(f"{API}/sites", json={"site_config": valid_site_config(), "demo_mode": True}).json()["job_id"]
    r = client.get(f"{API}/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert_matches("job_status.schema.json", body)
    assert body["job_id"] == job_id
    assert body["site_id"] == "kosi"
    assert body["kind"] == "onboarding"
    assert body["stage"] == "queued"
    assert body["demo_mode"] is True


def test_get_unknown_job_is_404():
    r = client.get(f"{API}/jobs/{JOB_ID}")
    assert r.status_code == 404
    assert_matches("error.schema.json", r.json()["detail"])
    assert r.json()["detail"]["error"]["code"] == "job_not_found"


def test_job_survives_api_restart():
    job_id = client.post(f"{API}/sites", json={"site_config": valid_site_config()}).json()["job_id"]
    import backend.m0_api.main as main_module

    restarted = importlib.reload(main_module)  # a fresh app: nothing carried over in memory
    r = TestClient(restarted.app).get(f"{API}/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["stage"] == "queued"


def test_job_fails_clearly_when_real_onboarding_inputs_are_missing():
    job_id = client.post(f"{API}/sites", json={"site_config": valid_site_config()}).json()["job_id"]
    worker = Worker()
    worker.acquire_lock()
    worker.recover()
    try:
        wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["stage"] == "failed", worker.tick)
    finally:
        worker.close()
    body = client.get(f"{API}/jobs/{job_id}").json()
    assert_matches("job_status.schema.json", body)
    assert body["started_at"] is not None
    # Feature 2: the terrain stage now ingests data itself. With network ingestion disabled
    # (tests/m0_api/conftest.py) no raw data can be obtained, and the job says exactly that.
    assert body["error"]["error"]["code"] == "terrain_failed"
    assert "network ingestion is disabled (SIH26_INGEST_NETWORK=off)" in body["error"]["error"]["message"]


# =============================================================================
# 7-8. recheck, rerun
# =============================================================================
def test_set_recheck():
    r = client.put(f"{API}/sites/{KNOWN_SITE}/recheck", json={"frequency_days": 30})
    assert r.status_code == 200
    body = r.json()
    assert_matches("site_summary.schema.json", body)
    assert body["recheck"]["frequency_days"] == 30


def test_set_recheck_persists_and_shows_up_in_get_sites():
    client.put(f"{API}/sites/{KNOWN_SITE}/recheck", json={"frequency_days": 45})
    listing = client.get(f"{API}/sites").json()
    entry = next(s for s in listing if s["site_id"] == KNOWN_SITE)
    assert entry["recheck"]["frequency_days"] == 45


def test_set_recheck_unknown_site_404():
    r = client.put(f"{API}/sites/{UNKNOWN_SITE}/recheck", json={"frequency_days": 30})
    assert r.status_code == 404


def test_get_sites_reflects_outdated_flag():
    site_status.record_check(KNOWN_SITE, outdated=True, status_reason_key="outdated_lake_area_change",
                              status_detail={"change_pct": 12.0, "threshold_pct": 10.0})
    listing = client.get(f"{API}/sites").json()
    entry = next(s for s in listing if s["site_id"] == KNOWN_SITE)
    assert entry["status"] == "outdated"
    assert entry["status_reason_key"] == "outdated_lake_area_change"

    detail = client.get(f"{API}/sites/{KNOWN_SITE}").json()
    assert_matches("site_detail.schema.json", detail)
    assert detail["status"] == "outdated"


def test_rerun_site():
    r = client.post(f"{API}/sites/{KNOWN_SITE}/rerun")
    assert r.status_code == 202
    body = r.json()
    assert_matches("job_accepted.schema.json", body)
    conn = registry.connect()
    try:
        row = jobs.get_job(conn, body["job_id"])
    finally:
        conn.close()
    assert row["kind"] == "onboarding"
    assert row["site_id"] == KNOWN_SITE


def test_rerun_site_conflicts_with_active_job():
    first = client.post(f"{API}/sites/{KNOWN_SITE}/rerun").json()
    second = client.post(f"{API}/sites/{KNOWN_SITE}/rerun")
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["details"]["job_id"] == first["job_id"]


def test_rerun_site_unknown_site_404():
    r = client.post(f"{API}/sites/{UNKNOWN_SITE}/rerun")
    assert r.status_code == 404


# =============================================================================
# 9-10. flood query
# =============================================================================
VALID_FLOOD_QUERY = {
    "site_id": KNOWN_SITE,
    "model": "delft3d",
    "mode": "unknown_breach",
    "inputs": {"breach_width_m": {"type": "exact", "value": 72.0}, "failure_time_s": {"type": "slider", "position": 3}},
    "options": {"n_samples": 500, "seed": None},
}


def test_post_flood_query():
    r = client.post(f"{API}/flood/query", json=VALID_FLOOD_QUERY)
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "scenario_id_required"


def test_post_flood_query_unknown_site_404():
    r = client.post(f"{API}/flood/query", json={**VALID_FLOOD_QUERY, "site_id": UNKNOWN_SITE})
    assert r.status_code == 404


def test_post_flood_query_bad_mode_422():
    r = client.post(f"{API}/flood/query", json={**VALID_FLOOD_QUERY, "mode": "not_a_mode"})
    assert r.status_code == 422
    assert_matches("error.schema.json", r.json()["detail"])


def test_get_flood_by_query_id():
    r = client.get(f"{API}/flood/{QUERY_ID}")
    assert r.status_code == 404


def test_real_registered_run_query_uses_rasters_and_renders_them(data_dir):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    site_dir = data_dir / KNOWN_SITE
    run_id, scenario_id = "s001__delft3d", "s001"
    run_dir = site_dir / "runs" / run_id
    summary = run_dir / "summary"
    summary.mkdir(parents=True)
    profile = {"driver":"GTiff", "height":2, "width":3, "count":1, "dtype":"float32",
               "crs":"EPSG:32645", "transform":from_origin(500000, 3100000, 30, 30), "nodata":-9999.0}
    arrays = {"max_depth.tif":np.array([[0.0, 0.4, 1.5], [0.0, 2.0, -9999.0]], dtype="float32"),
              "max_velocity.tif":np.array([[0.0, 0.5, 2.0], [0.0, 3.0, -9999.0]], dtype="float32"),
              "arrival_time.tif":np.array([[-9999.0, 60.0, 120.0], [-9999.0, 180.0, -9999.0]], dtype="float32")}
    for name, values in arrays.items():
        with rasterio.open(summary / name, "w", **profile) as ds:
            ds.write(values, 1)
    # Synthetic fixture of an ACCEPTED run (Feature 13: only accepted runs are queryable).
    (run_dir / "run_meta.json").write_text(json.dumps({"run_id":run_id,"scenario_id":scenario_id,
                                                       "thresholds":{"extent_m":0.3},
                                                       "acceptance":{"status":"ACCEPTED"}, "run_class":"accepted"}))
    with registry.connect() as conn:
        conn.execute("INSERT INTO scenarios VALUES (?,?,?,?,?)", (scenario_id, KNOWN_SITE, "design",
                     json.dumps({"breach_width_m":42.0}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id,scenario_id,model,status,run_dir,meta_json) VALUES (?,?,?,?,?,?)",
                     (run_id, scenario_id, "delft3d", "postprocessed", str(run_dir),
                      json.dumps({"has_placeholders":False,"placeholder_fields":[]})))
    response = client.post(f"{API}/flood/query", json={"site_id":KNOWN_SITE,"model":"delft3d",
                       "mode":"scenario","scenario_id":scenario_id,"inputs":{}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert_matches("flood_query_response.schema.json", body)
    assert body["method"] == "delft3d_direct"
    assert body["summary"]["max_depth_m"]["value"] == 2.0
    assert body["summary"]["inundated_area_m2"]["value"] == 2700.0
    assert body["provenance"]["run_ids"] == [run_id]
    assert body["confidence"]["overall"]["level"] == "LOW"
    polled = client.get(f"{API}/flood/{body['query_id']}")
    assert polled.status_code == 200 and polled.json() == body
    png = client.get(f"{API}/flood/{body['query_id']}/layers/depth_p50.png")
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    extent = client.get(f"{API}/flood/{body['query_id']}/extent.geojson")
    assert extent.status_code == 200
    assert extent.json()["features"][0]["properties"]["source_run_id"] == run_id


def test_real_query_missing_registered_run_does_not_use_example():
    response = client.post(f"{API}/flood/query", json={"site_id":KNOWN_SITE,"model":"delft3d",
                            "mode":"scenario","scenario_id":"missing_s001","inputs":{}})
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "real_run_not_found"


# =============================================================================
# 11-13. layers, extent, timeline
# =============================================================================
def test_get_flood_layer_png():
    r = client.get(f"{API}/flood/{QUERY_ID}/layers/p_inundation.png")
    assert r.status_code == 404


def test_get_flood_layer_non_png_400():
    r = client.get(f"{API}/flood/{QUERY_ID}/layers/p_inundation.tif")
    assert r.status_code == 400


def test_get_flood_layer_renders_real_geotiff_when_present(data_dir):
    """A GeoTIFF at the contract §1.8 layers path is rendered for real
    (backend/m0_api/rendering.py), not served as the 1x1 mock PNG."""
    import numpy as np

    from backend.shared.grid import CanonicalGrid, write_grid_raster

    grid = CanonicalGrid(
        site_id=KNOWN_SITE, grid_id="farfield", crs_epsg=32645,
        origin_x=500_000.0, origin_y=3_100_000.0, cell_size_m=30.0, width=4, height=3,
    )
    layers_dir = data_dir / KNOWN_SITE / "queries" / QUERY_ID / "layers"
    layers_dir.mkdir(parents=True)
    write_grid_raster(layers_dir / "depth_p50.tif", np.full(grid.shape, 1.0, dtype=np.float32), grid)

    r = client.get(f"{API}/flood/{QUERY_ID}/layers/depth_p50.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert r.content != mock_files.mock_png()
    assert (layers_dir / "depth_p50.png").is_file()  # cached alongside the source .tif


def test_get_flood_extent_geojson():
    r = client.get(f"{API}/flood/{QUERY_ID}/extent.geojson")
    assert r.status_code == 404


def test_get_flood_timeline():
    r = client.get(f"{API}/flood/{QUERY_ID}/timeline")
    assert r.status_code == 404


def _write_synthetic_timeline(data_dir, *, query_id=QUERY_ID, site_id=KNOWN_SITE, t_end_s=3600.0, width=6, height=3):
    """Writes real timeline/*.tif + timeline_data.json under `data_dir`, the
    way M5's `write_timeline_inputs` would after a real query -- the M0
    tests then only exercise the route, not the M5 arithmetic (covered in
    tests/m5_emulator/test_timeline.py)."""
    import dataclasses

    import numpy as np

    from backend.m2_breach.hydrograph import triangular
    from backend.m5_emulator import timeline as m5_timeline
    from backend.m5_emulator.query import FloodResult
    from backend.shared.grid import CanonicalGrid

    grid = CanonicalGrid(
        site_id=site_id, grid_id="farfield", crs_epsg=32645,
        origin_x=500_000.0, origin_y=3_100_000.0, cell_size_m=30.0, width=width, height=height,
    )
    col = np.tile(np.arange(width), (height, 1)).astype(np.float32)
    frac = col / (width - 1)  # 0 (upstream) -> 1 (downstream)
    p50 = frac * t_end_s * 0.6
    p10 = np.maximum(p50 - 300.0, 0.0)
    p90 = p50 + 300.0
    extent_class = np.where(frac < 0.7, np.uint8(2), np.uint8(1))

    result = FloodResult(
        site_id=site_id, model="synthetic", mode="scenario", resolved_inputs={},
        p_inundation=np.ones(grid.shape, dtype=np.float32), extent_class=extent_class,
        median={"arrival_time": p50}, p10={"arrival_time": p10}, p90={"arrival_time": p90},
        poi_depth={}, poi_velocity={}, poi_arrival={}, poi_p_inundation={},
        inundated_area_m2=(0.0, 0.0, 0.0), max_depth_site=(0.0, 0.0, 0.0), max_velocity_site=(0.0, 0.0, 0.0),
        outside_trained_range=False, confidence={"overall": {"level": "MODERATE"}}, n_samples=None,
    )
    chainage_m, cell_index = np.arange(width) * grid.cell_size_m, np.arange(width)
    hg = dataclasses.replace(triangular(Q_p=500.0, V=2_000_000.0, T_f=600.0), dam_id="synth_dam")

    query_dir = data_dir / site_id / "queries" / query_id
    m5_timeline.write_timeline_inputs(
        result, grid, query_dir, hydrographs=[hg], chainage_m=chainage_m, cell_index=cell_index,
        pois={}, t_end_s=t_end_s, contract_version="0.3.0", created_at="2026-09-24T10:15:00Z",
    )
    return query_dir


def test_get_flood_timeline_real_when_written(data_dir):
    _write_synthetic_timeline(data_dir)
    r = client.get(f"{API}/flood/{QUERY_ID}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert_matches("timeline.schema.json", body)
    assert body["interval_s"] == 300
    assert body["t_end_s"] == 3600.0
    assert body["frames"]
    assert any(row["arrival_p50_s"] is not None for row in body["arrival_profile"])
    assert body["hydrographs"][0]["dam_id"] == "synth_dam"
    assert any(c["id"] == "arrival_depth_not_joint" for c in body["caveats"])


def test_get_flood_timeline_interval_s_changes_frame_count(data_dir):
    _write_synthetic_timeline(data_dir)
    coarse = client.get(f"{API}/flood/{QUERY_ID}/timeline", params={"interval_s": 1800}).json()
    fine = client.get(f"{API}/flood/{QUERY_ID}/timeline", params={"interval_s": 300}).json()
    assert len(fine["frames"]) > len(coarse["frames"])
    assert fine["frames"][-1]["t_s"] <= fine["t_end_s"]


def test_get_flood_timeline_too_many_frames_422(data_dir):
    _write_synthetic_timeline(data_dir, t_end_s=100_000.0)
    r = client.get(f"{API}/flood/{QUERY_ID}/timeline", params={"interval_s": 60})
    assert r.status_code == 422


def test_get_flood_timeline_interval_s_out_of_range_422():
    r = client.get(f"{API}/flood/{QUERY_ID}/timeline", params={"interval_s": 10})
    assert r.status_code == 422


def test_get_flood_timeline_frame_png_renders_and_caches(data_dir):
    query_dir = _write_synthetic_timeline(data_dir)
    r = client.get(f"{API}/files/{KNOWN_SITE}/queries/{QUERY_ID}/timeline/median_t300.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert r.content != mock_files.mock_png()
    assert (query_dir / "timeline" / "median_t300.png").is_file()


def test_get_flood_timeline_frame_png_high_and_possible_differ(data_dir):
    _write_synthetic_timeline(data_dir)
    high = client.get(f"{API}/files/{KNOWN_SITE}/queries/{QUERY_ID}/timeline/high_t3600.png")
    possible = client.get(f"{API}/files/{KNOWN_SITE}/queries/{QUERY_ID}/timeline/possible_t3600.png")
    assert high.status_code == possible.status_code == 200
    assert high.content != possible.content


def test_get_flood_timeline_frame_png_is_404_when_no_query():
    # Feature 12: a missing artifact is a 404, never a placeholder PNG.
    r = client.get(f"{API}/files/{KNOWN_SITE}/queries/{QUERY_ID}/timeline/median_t300.png")
    assert r.status_code == 404


def test_get_flood_timeline_frame_path_rejects_traversal(data_dir):
    _write_synthetic_timeline(data_dir)
    r = client.get(f"{API}/files/{KNOWN_SITE}/queries/../../../etc/timeline/median_t300.png")
    # doesn't match TIMELINE_FRAME_PATH_RE (query_id pattern fails) -> falls through to the generic mock/404 path
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.content == mock_files.mock_png()


# =============================================================================
# 14. impact
# =============================================================================
def test_get_impact():
    r = client.get(f"{API}/impact/{QUERY_ID}")
    assert r.status_code == 404


# =============================================================================
# 15. compare
# =============================================================================
def test_get_compare():
    r = client.get(f"{API}/compare/{KNOWN_SITE}")
    assert r.status_code == 200
    assert_matches("compare.schema.json", r.json())


def test_get_compare_with_scenario_id():
    r = client.get(f"{API}/compare/{KNOWN_SITE}", params={"scenario_id": "teesta__s009"})
    assert r.status_code == 200
    body = r.json()
    assert_matches("compare.schema.json", body)
    assert body["scenario_id"] == "teesta__s009"


def _write_synthetic_compare(data_dir, *, site_id=KNOWN_SITE, model="delft3d", scenario_id="teesta_s005"):
    """Writes a real compare sidecar + depth_diff.tif under `data_dir`, the
    way M5's `write_compare_inputs` would after a real LOOCV run -- a small
    (N=6, coarse-grid, n_restarts=1) synthetic library keeps the one real GP
    fit this triggers fast. The M0 tests then only exercise the route, not
    the M5 arithmetic (covered in tests/m5_emulator/test_compare.py)."""
    from backend.m5_emulator import compare as m5_compare
    from backend.m5_emulator import library as lib
    from backend.m5_emulator.emulator import EmulatorSettings
    from backend.m5_emulator.inputs import make_input_specs
    from backend.shared.grid import CanonicalGrid

    grid = CanonicalGrid(
        site_id=site_id, grid_id="farfield", crs_epsg=32645,
        origin_x=500_000.0, origin_y=3_100_000.0, cell_size_m=250.0, width=160, height=12,
    )
    library = lib.build_synthetic_library(grid, n=6, seed=5)
    ranges = {
        name: (float(library.X_raw[:, i].min()), float(library.X_raw[:, i].max()))
        for i, name in enumerate(lib.INPUT_ORDER)
    }
    specs = make_input_specs(ranges)
    settings = EmulatorSettings(seed=5, n_restarts=1)
    maps = {"max_depth": library.max_depth, "max_velocity": library.max_velocity, "arrival_time": library.arrival_time}

    held_out_run_id = f"{scenario_id}__{model}"
    report = {
        "per_run": [{
            "run_id": held_out_run_id, "iou": 0.42, "depth_rmse_wet_m": 0.11, "arrival_mae_s": 123.0,
        }],
        "summary": {"extent": {"iou_median": 0.42}, "arrival": {"mae_s_median": 123.0}},
        "baseline_linear": {"extent": {"iou_median": 0.20}, "arrival": {"mae_s_median": 400.0}},
    }

    out_dir = data_dir / site_id / "emulator" / model / "validation"
    compare_dir = m5_compare.write_compare_inputs(
        report, library.X_raw, maps, library.grid, specs, [held_out_run_id] + library.run_ids[1:], library.t_end_s,
        settings, held_out_run_id, out_dir, contract_version="0.3.0", created_at="2026-09-25T00:00:00Z",
    )
    return compare_dir


def test_get_compare_real_when_written(data_dir):
    _write_synthetic_compare(data_dir)
    r = client.get(f"{API}/compare/{KNOWN_SITE}", params={"scenario_id": "teesta_s005"})
    assert r.status_code == 200
    body = r.json()
    assert_matches("compare.schema.json", body)
    assert body["emulator_vs_physics"]["held_out_run_id"] == "teesta_s005__delft3d"
    assert body["emulator_vs_physics"]["metrics"]["iou"] == 0.42
    assert body["gp_vs_linear"]["iou_median_gp"] == 0.42
    assert body["gp_vs_linear"]["iou_median_linear"] == 0.20
    assert body["emulator_vs_physics"]["layers"][0]["style_id"] == "depth_diff"
    # sph_vs_delft3d must never be decorated with the contract example's fictitious zero-metric
    # numbers just because a real emulator_vs_physics sidecar exists -- no paired SPH/D-Flow FM
    # near-field run exists for this scenario, so it stays an honest, explicit unavailable state.
    assert body["sph_vs_delft3d"]["available"] is False
    assert body["sph_vs_delft3d"]["run_ids"] == []
    assert body["sph_vs_delft3d"]["metrics"] == {}
    assert "teesta_s005__delft3d" in body["sph_vs_delft3d"]["unavailable_reason"]
    assert "teesta_s005__sph" in body["sph_vs_delft3d"]["unavailable_reason"]
    assert any(c["id"] == "synthetic_world_not_real_physics" for c in body["caveats"])


def test_get_compare_falls_back_to_mock_without_scenario_id(data_dir):
    _write_synthetic_compare(data_dir)
    r = client.get(f"{API}/compare/{KNOWN_SITE}")
    assert r.status_code == 200
    body = r.json()
    assert body["emulator_vs_physics"]["held_out_run_id"] != "teesta_s005__delft3d"


def test_get_compare_falls_back_to_mock_when_no_sidecar_written():
    r = client.get(f"{API}/compare/{KNOWN_SITE}", params={"scenario_id": "teesta_s999"})
    assert r.status_code == 200
    body = r.json()
    assert_matches("compare.schema.json", body)


def test_real_solver_site_compare_is_unavailable_without_a_paired_sph_artifact(data_dir):
    run_dir = data_dir / KNOWN_SITE / "runs" / "teesta_2023_mvp__delft3d"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({
        "run_id": "teesta_2023_mvp__delft3d", "scenario_id": "teesta_2023_mvp",
        "solver_status": "REAL_SOLVER_OUTPUT", "model": "delft3d",
    }))
    response = client.get(f"{API}/compare/{KNOWN_SITE}", params={"scenario_id": "teesta_2023_mvp"})
    assert response.status_code == 200
    body = response.json()
    assert_matches("compare.schema.json", body)
    assert body["sph_vs_delft3d"]["available"] is False
    assert body["sph_vs_delft3d"]["run_ids"] == ["teesta_2023_mvp__delft3d"]
    assert body["gp_vs_linear"] == {}


def test_get_compare_defaults_to_registered_teesta_mvp_pair(data_dir):
    compare_dir = data_dir / KNOWN_SITE / "compare" / "teesta_2023_mvp"
    compare_dir.mkdir(parents=True)
    example = Path(__file__).resolve().parents[2] / "contracts/examples/compare.example.json"
    sidecar = json.loads(example.read_text())
    sidecar["site_id"] = KNOWN_SITE
    sidecar["scenario_id"] = "teesta_2023_mvp"
    sidecar["sph_vs_delft3d"]["available"] = True
    sidecar["sph_vs_delft3d"]["run_ids"] = [
        "teesta_2023_mvp__delft3d", "teesta_2023_mvp__dualsphysics",
    ]
    (compare_dir / "compare.json").write_text(json.dumps(sidecar))

    response = client.get(f"{API}/compare/{KNOWN_SITE}")
    assert response.status_code == 200
    body = response.json()
    assert_matches("compare.schema.json", body)
    assert body["scenario_id"] == "teesta_2023_mvp"
    assert body["sph_vs_delft3d"]["run_ids"] == [
        "teesta_2023_mvp__delft3d", "teesta_2023_mvp__dualsphysics",
    ]


def test_get_compare_diff_png_renders_and_caches(data_dir):
    compare_dir = _write_synthetic_compare(data_dir)
    r = client.get(f"{API}/files/{KNOWN_SITE}/emulator/delft3d/validation/compare/teesta_s005__delft3d__depth_diff.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert r.content != mock_files.mock_png()
    assert (compare_dir / "teesta_s005__delft3d__depth_diff.png").is_file()


def test_get_compare_diff_png_is_404_when_no_sidecar():
    r = client.get(f"{API}/files/{KNOWN_SITE}/emulator/delft3d/validation/compare/nope__delft3d__depth_diff.png")
    assert r.status_code == 404


# =============================================================================
# 16-17. validation
# =============================================================================
def test_get_validation():
    r = client.get(f"{API}/validation/{KNOWN_SITE}")
    assert r.status_code == 200
    body = r.json()
    assert_matches("validation.schema.json", body)
    assert body["site_id"] == KNOWN_SITE


def test_get_historical_validation():
    r = client.get(f"{API}/validation/{KNOWN_SITE}", params={"event": EVENT_ID})
    assert r.status_code == 200
    body = r.json()
    assert_matches("historical_validation.schema.json", body)
    assert body["event_id"] == EVENT_ID


def test_real_solver_site_has_no_fabricated_historical_validation(data_dir):
    run_dir = data_dir / KNOWN_SITE / "runs" / "teesta_2023_mvp__delft3d"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({"solver_status": "REAL_SOLVER_OUTPUT"}))
    response = client.get(f"{API}/validation/{KNOWN_SITE}", params={"event": EVENT_ID})
    assert response.status_code == 200
    body = response.json()
    assert_matches("historical_validation.schema.json", body)
    assert body["metrics"] == {}
    assert body["provenance"]["validation_available"] is False


# The real Teesta site config (sites/teesta.yaml) declares event id "sikkim_glof_2023", not the
# contract's generic "teesta_2023" example. Task D (Validation tab) reads the real event id from
# the site config rather than hardcoding the contract's placeholder string.
REAL_EVENT_ID = "sikkim_glof_2023"


def test_get_validation_lists_real_site_events(data_dir):
    run_dir = data_dir / KNOWN_SITE / "runs" / "teesta_2023_mvp__delft3d"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({"solver_status": "REAL_SOLVER_OUTPUT", "run_id": "teesta_2023_mvp__delft3d"}))
    response = client.get(f"{API}/validation/{KNOWN_SITE}")
    assert response.status_code == 200
    body = response.json()
    assert_matches("validation.schema.json", body)
    assert body["events"] == [REAL_EVENT_ID]


def test_get_validation_synthetic_loocv_present(data_dir, monkeypatch, tmp_path):
    run_dir = data_dir / KNOWN_SITE / "runs" / "teesta_2023_mvp__delft3d"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({"solver_status": "REAL_SOLVER_OUTPUT", "run_id": "teesta_2023_mvp__delft3d"}))
    report = tmp_path / "loocv.json"
    report.write_text(json.dumps({"model": "synthetic", "n_runs": 30, "summary": {"extent": {"grade": "A"}},
                                   "grade_thresholds_ref": "docs/m5_spec.md"}))
    monkeypatch.setenv("SIH26_M5_SYNTHETIC_LOOCV_REPORT", str(report))
    response = client.get(f"{API}/validation/{KNOWN_SITE}")
    body = response.json()
    assert_matches("validation.schema.json", body)
    assert body["synthetic_loocv"]["world"] == "synthetic_test_world"
    assert body["synthetic_loocv"]["n_runs"] == 30


def test_get_validation_synthetic_loocv_absent(data_dir, monkeypatch, tmp_path):
    run_dir = data_dir / KNOWN_SITE / "runs" / "teesta_2023_mvp__delft3d"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({"solver_status": "REAL_SOLVER_OUTPUT", "run_id": "teesta_2023_mvp__delft3d"}))
    monkeypatch.setenv("SIH26_M5_SYNTHETIC_LOOCV_REPORT", str(tmp_path / "nope.json"))
    response = client.get(f"{API}/validation/{KNOWN_SITE}")
    body = response.json()
    assert_matches("validation.schema.json", body)
    assert body["synthetic_loocv"] is None


def test_get_historical_validation_real_event_literature_comparison(data_dir):
    run_id = "teesta_2023_mvp__delft3d"
    run_dir = data_dir / KNOWN_SITE / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({
        "solver_status": "REAL_SOLVER_OUTPUT", "run_id": run_id,
        "forcing_provenance_path": "breach/hydrographs/teesta_2023_mvp__south_lhonak.json",
        "thresholds": {"extent_m": 0.3},
    }))
    hydrograph_dir = data_dir / KNOWN_SITE / "breach" / "hydrographs"
    hydrograph_dir.mkdir(parents=True)
    (hydrograph_dir / "teesta_2023_mvp__south_lhonak.json").write_text(json.dumps({
        "provenance": {
            "source_constraints": {
                "peak_time_ist": {"value": "2023-10-04T03:20:00+05:30", "status": "MVP reconstruction target"},
            },
            "construction": {"duration_s": 14587.892049598833},
        }
    }))
    with (run_dir / "timeseries.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["poi_id", "t_s", "depth_m", "velocity_ms", "wse_m", "arrival_s_since_t0"])
        writer.writerow(["teesta_pilot__poi__chungthang", "0.0", "0.0", "0.0", "1583.17", ""])
        writer.writerow(["teesta_pilot__poi__chungthang", "23340.0", "11.6", "0.39", "1594.77", "22920.0"])
    response = client.get(f"{API}/validation/{KNOWN_SITE}", params={"event": REAL_EVENT_ID})
    assert response.status_code == 200
    body = response.json()
    assert_matches("historical_validation.schema.json", body)
    assert body["event_id"] == REAL_EVENT_ID
    assert body["observed"]["available"] is False
    assert "not yet digitized" in body["observed"]["note"]
    assert body["literature_comparison"]["available"] is True
    assert body["literature_comparison"]["simulated"]["arrival_s_since_t0"] == 22920.0
    assert "arrival_time_ist_estimate" in body["literature_comparison"]["simulated"]
    # Feature 12: literature reconstructions are a comparison, never validation.
    assert body["provenance"]["validation_available"] is False
    assert body["provenance"]["comparison_available"] is True
    assert body["provenance"]["comparison_kind"] == "literature_reconstruction"
    assert body["literature_comparison"]["is_validation"] is False
    assert any(c["id"] == "no_observed_extent" for c in body["caveats"])
    assert any(c["id"] == "literature_comparison_only" for c in body["caveats"])


def test_get_historical_validation_observed_extent_present(data_dir):
    run_id = "teesta_2023_mvp__delft3d"
    run_dir = data_dir / KNOWN_SITE / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text(json.dumps({"solver_status": "REAL_SOLVER_OUTPUT", "run_id": run_id}))
    observed_dir = data_dir / KNOWN_SITE / "observed"
    observed_dir.mkdir(parents=True)
    (observed_dir / "flood_extent_2023.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    response = client.get(f"{API}/validation/{KNOWN_SITE}", params={"event": REAL_EVENT_ID})
    body = response.json()
    assert_matches("historical_validation.schema.json", body)
    assert body["observed"]["available"] is True
    assert body["observed"]["extent_url"].endswith("flood_extent_2023.geojson")
    assert not any(c["id"] == "no_observed_extent" for c in body["caveats"])


# =============================================================================
# 18. export
# =============================================================================
def _register_export_query(data_dir, *, query_id=QUERY_ID, site_id=KNOWN_SITE) -> Path:
    """A minimal real registered query -- extent + result.json + a `queries` row -- so export
    tests exercise the real `m6_impact.exports` path instead of the removed mock fallback (main.py
    no longer fabricates export content for an unregistered query_id; see
    test_export_of_unregistered_query_id_is_404 below)."""
    query_dir = data_dir / site_id / "queries" / query_id
    query_dir.mkdir(parents=True)
    extent = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[88.0, 27.0], [88.1, 27.0], [88.1, 27.1], [88.0, 27.1], [88.0, 27.0]]]}}]}
    (query_dir / "extent.geojson").write_text(json.dumps(extent))
    (query_dir / "result.json").write_text(json.dumps({"flags": {"demo_mode": False}, "method": "delft3d_direct"}))
    with registry.connect() as conn:
        conn.execute("INSERT INTO queries (query_id,site_id,request_json,status,result_path,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (query_id, site_id, "{}", "complete",
                      str(query_dir / "result.json"), registry.utc_now()))
    return query_dir


@pytest.mark.parametrize(
    "fmt,content_type",
    [
        ("shp", "application/zip"),
        ("kml", "application/vnd.google-earth.kml+xml"),
        ("geojson", "application/geo+json"),
        ("pdf", "application/pdf"),
    ],
)
def test_export_formats(fmt, content_type, data_dir):
    _register_export_query(data_dir)
    r = client.get(f"{API}/export/{QUERY_ID}", params={"format": fmt})
    assert r.status_code == 200
    assert r.headers["content-type"] == content_type
    assert "attachment" in r.headers["content-disposition"]
    assert len(r.content) > 0


def test_export_shp_is_a_real_zip(data_dir):
    _register_export_query(data_dir)
    r = client.get(f"{API}/export/{QUERY_ID}", params={"format": "shp"})
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        assert zf.namelist()


def test_export_geojson_matches_schema(data_dir):
    _register_export_query(data_dir)
    r = client.get(f"{API}/export/{QUERY_ID}", params={"format": "geojson"})
    assert_matches("geojson_feature_collection.schema.json", json.loads(r.content))


def test_export_invalid_format_400():
    r = client.get(f"{API}/export/{QUERY_ID}", params={"format": "shx"})
    assert r.status_code == 400
    assert_matches("error.schema.json", r.json()["detail"])


def test_export_of_unregistered_query_id_is_404(data_dir):
    """No fabricated shapefile/KML/PDF branded with a real-looking query_id -- an id nothing was
    ever registered for (not in the `queries` table, no on-disk query dir) must 404 naming the
    missing id, for every export format, not silently serve mock content."""
    unregistered_id = "q_20260101T000000Z_ffffff"  # validly-formatted, never registered
    for fmt in ("shp", "kml", "geojson", "pdf"):
        r = client.get(f"{API}/export/{unregistered_id}", params={"format": fmt})
        assert r.status_code == 404, fmt
        body = r.json()
        assert_matches("error.schema.json", body["detail"])
        assert unregistered_id in body["detail"]["error"]["message"]


def test_export_kml_covers_every_part_of_a_multipolygon_extent_with_holes(data_dir):
    """A real flood extent is frequently a MultiPolygon (disjoint wet regions from an unconditioned
    DEM, docs/progress.md 2026-09-28), not a single simple Polygon. Exporting only
    coordinates[0] of the first feature (the earlier bug) silently drops every other part and
    every hole."""
    import xml.etree.ElementTree as ET

    query_id = "q_20260928T000000Z_abc123"
    site_dir = data_dir / KNOWN_SITE
    query_dir = site_dir / "queries" / query_id
    query_dir.mkdir(parents=True)
    extent = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
        "geometry": {"type": "MultiPolygon", "coordinates": [
            [[[88.0, 27.0], [88.1, 27.0], [88.1, 27.1], [88.0, 27.1], [88.0, 27.0]],
             [[88.02, 27.02], [88.02, 27.04], [88.04, 27.04], [88.04, 27.02], [88.02, 27.02]]],
            [[[88.5, 27.5], [88.6, 27.5], [88.6, 27.6], [88.5, 27.6], [88.5, 27.5]]],
        ]}}]}
    (query_dir / "extent.geojson").write_text(json.dumps(extent))
    (query_dir / "result.json").write_text(json.dumps({"flags": {"demo_mode": False}}))
    with registry.connect() as conn:
        conn.execute("INSERT INTO queries (query_id,site_id,request_json,status,result_path,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (query_id, KNOWN_SITE, "{}", "complete",
                      str(query_dir / "result.json"), registry.utc_now()))

    r = client.get(f"{API}/export/{query_id}", params={"format": "kml"})
    assert r.status_code == 200
    root = ET.fromstring(r.content)  # raises if the KML is not well-formed XML
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    polygons = root.findall(".//k:Polygon", ns)
    assert len(polygons) == 2  # one per MultiPolygon part
    assert len(root.findall(".//k:innerBoundaryIs", ns)) == 1  # the hole in the first part
    outer_coords = polygons[0].find(".//k:outerBoundaryIs//k:coordinates", ns).text
    assert "88.5,27.5" not in outer_coords  # each polygon keeps only its own ring, not another's


# =============================================================================
# 19-20. gee
# =============================================================================
def test_get_gee(data_dir):  # Feature 12: isolated data dir, never the repo's data/
    r = client.get(f"{API}/gee/{KNOWN_SITE}")
    assert r.status_code == 200
    body = r.json()
    assert_matches("gee_layers.schema.json", body)
    assert body["site_id"] == KNOWN_SITE


def test_refresh_gee(data_dir, monkeypatch):  # Feature 12: isolated data dir
    # Forces the existing-cache fallback path (contract §4.8) regardless of whether this machine
    # happens to have real Earth Engine credentials set up -- this is a fast, offline unit test,
    # not a live-EE integration test (M7's own live fetch is exercised manually, CLAUDE.md rule 2).
    from backend.m7_gee import scene_search

    def _never(*a, **k):
        raise RuntimeError("live Earth Engine disabled for this test")

    monkeypatch.setattr(scene_search, "_ee_initialize", _never)
    r = client.post(f"{API}/gee/{KNOWN_SITE}/refresh")
    assert r.status_code == 200
    assert_matches("gee_layers.schema.json", r.json())


def test_get_gee_unknown_site_404():
    r = client.get(f"{API}/gee/{UNKNOWN_SITE}")
    assert r.status_code == 404


# =============================================================================
# 21. scene3d
# =============================================================================
def test_get_scene3d_without_artifacts_is_404():
    # Feature 12: no example scene served as real.
    r = client.get(f"{API}/scene3d/{QUERY_ID}")
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "scene_unavailable"


# =============================================================================
# 22. files
# =============================================================================
def test_get_file_png_missing_is_404():
    r = client.get(f"{API}/files/teesta/queries/{QUERY_ID}/layers/p_inundation.png")
    assert r.status_code == 404


def test_get_file_geojson():
    r = client.get(f"{API}/files/teesta/observed/flood_extent_2023.geojson")
    # A path matching the observed-extent artifact route must not silently
    # return an unrelated example when the real file is absent.
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "file_not_found"


def test_get_file_bin():
    r = client.get(f"{API}/files/teesta/queries/{QUERY_ID}/scene3d/terrain.bin")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"


def test_get_file_unknown_extension_404():
    r = client.get(f"{API}/files/teesta/raw/readme.txt")
    assert r.status_code == 404
    assert_matches("error.schema.json", r.json()["detail"])

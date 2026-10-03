"""`real_query.resolve_registered_run` surfaces the DEM-pit/steep-reach caveats end to end
(docs/progress.md 2026-09-27 "Teesta MVP dashboard stabilization" D-Flow headline investigation).
Uses the `data_dir` autouse fixture from `tests/m0_api/conftest.py`."""
from __future__ import annotations

import json

import numpy as np
import rasterio
from rasterio.transform import from_origin

from backend.m0_api import real_query, registry
from backend.shared.grid import FLOAT_NODATA


def test_direct_delft3d_query_flags_dem_pit_and_steep_reach(data_dir):
    site_dir = data_dir / "teesta"
    run_id, scenario_id = "s002__delft3d", "s002"
    run_dir = site_dir / "runs" / run_id
    summary = run_dir / "summary"
    summary.mkdir(parents=True)
    # 30 m summary grid, 2x2, matching the extent of the finer DEM below exactly.
    depth_profile = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32",
                      "crs": "EPSG:32645", "transform": from_origin(500000, 3100000, 30, 30),
                      "nodata": -9999.0}
    # cell (0,0) [x:500000-500030, y:3099970-3100000] is the deepest cell -> should land in the pit.
    # cell (0,1) [x:500030-500060, y:3099970-3100000] carries the fastest velocity -> steep reach.
    depth = np.array([[5.0, 1.0], [0.4, 0.5]], dtype="float32")
    velocity = np.array([[1.0, 8.0], [0.5, 0.6]], dtype="float32")
    arrival = np.array([[60.0, 90.0], [120.0, 150.0]], dtype="float32")
    for name, arr in (("max_depth.tif", depth), ("max_velocity.tif", velocity),
                      ("arrival_time.tif", arrival)):
        with rasterio.open(summary / name, "w", **depth_profile) as ds:
            ds.write(arr, 1)

    # 10 m DEM covering the same 60x60 m extent, so the pit and step land inside real interior
    # cells (not on the DEM's own boundary, which `backend.m1_terrain.hydro.route` treats as an
    # outlet and never flags).
    dem = np.full((6, 6), 100.0, dtype=np.float32)
    dem[1, 1] = 90.0  # 10 m pit under summary cell (0,0)'s south-west corner
    dem[1:4, 3:6] = 150.0  # a sharp step under summary cell (0,1)
    dem_profile = {"driver": "GTiff", "height": 6, "width": 6, "count": 1, "dtype": "float32",
                   "crs": "EPSG:32645", "transform": from_origin(500000, 3100000, 10, 10),
                   "nodata": FLOAT_NODATA}
    terrain_dir = site_dir / "terrain"
    terrain_dir.mkdir(parents=True)
    with rasterio.open(terrain_dir / "dem.tif", "w", **dem_profile) as ds:
        ds.write(dem, 1)

    (run_dir / "run_meta.json").write_text(json.dumps(
        {"run_id": run_id, "scenario_id": scenario_id, "thresholds": {"extent_m": 0.3},
         "acceptance": {"status": "ACCEPTED"}, "run_class": "accepted"}))  # synthetic accepted-run fixture
    with registry.connect() as conn:
        conn.execute("INSERT INTO scenarios VALUES (?,?,?,?,?)",
                     (scenario_id, "teesta", "design", json.dumps({}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id,scenario_id,model,status,run_dir,meta_json) "
                     "VALUES (?,?,?,?,?,?)",
                     (run_id, scenario_id, "delft3d", "postprocessed", str(run_dir),
                      json.dumps({"has_placeholders": False, "placeholder_fields": []})))

    result = real_query.resolve_registered_run("teesta", scenario_id, "delft3d",
                                                "q_test_0001", {"inputs": {}}, data_dir=data_dir)

    caveat_ids = {c["id"] for c in result["caveats"]}
    assert "dem_depression_ponding" in caveat_ids
    assert "clear_water_steep_reach_velocity" in caveat_ids
    diagnostics = result["provenance"]["diagnostics"]
    assert diagnostics["max_depth_cell_in_dem_pit"] is True
    assert diagnostics["max_velocity_cell_on_steep_reach"] is True
    assert 0.0 < diagnostics["dem_pit_fraction_of_wet_area"] <= 1.0


def test_direct_solver_bounds_latlng_is_lat_first(data_dir):
    """Regression for docs/progress.md 2026-09-28 "STEP 2": resolve_registered_run's
    layers[].bounds_latlng, on a Teesta-realistic UTM 45N summary raster (~28N, ~87E -- a
    magnitude check can't tell these apart), must come back contract-ordered
    [[south,west],[north,east]], not the axis-swapped order this path used to emit."""
    site_dir = data_dir / "teesta"
    run_id, scenario_id = "s003__delft3d", "s003"
    run_dir = site_dir / "runs" / run_id
    summary = run_dir / "summary"
    summary.mkdir(parents=True)
    depth_profile = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32",
                      "crs": "EPSG:32645", "transform": from_origin(500000, 3100000, 30, 30),
                      "nodata": -9999.0}
    for name in ("max_depth.tif", "max_velocity.tif", "arrival_time.tif"):
        with rasterio.open(summary / name, "w", **depth_profile) as ds:
            ds.write(np.full((2, 2), 1.0, dtype="float32"), 1)

    (run_dir / "run_meta.json").write_text(json.dumps(
        {"run_id": run_id, "scenario_id": scenario_id, "thresholds": {"extent_m": 0.3},
         "acceptance": {"status": "ACCEPTED"}, "run_class": "accepted"}))  # synthetic accepted-run fixture
    with registry.connect() as conn:
        conn.execute("INSERT INTO scenarios VALUES (?,?,?,?,?)",
                     (scenario_id, "teesta", "design", json.dumps({}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id,scenario_id,model,status,run_dir,meta_json) "
                     "VALUES (?,?,?,?,?,?)",
                     (run_id, scenario_id, "delft3d", "postprocessed", str(run_dir),
                      json.dumps({"has_placeholders": False, "placeholder_fields": []})))

    result = real_query.resolve_registered_run("teesta", scenario_id, "delft3d",
                                                "q_test_0002", {"inputs": {}}, data_dir=data_dir)

    depth_layer = next(l for l in result["layers"] if l["layer_id"] == "depth_p50")
    (south, west), (north, east) = depth_layer["bounds_latlng"]
    assert 27.0 < south < north < 29.0, f"expected latitudes ~27-29N, got south={south}, north={north}"
    assert 86.0 < west < east < 88.0, f"expected longitudes ~86-88E, got west={west}, east={east}"


def test_direct_sph_query_skips_dem_diagnostics(data_dir):
    """The DEM classifier only applies to the delft3d path (SPH's near-field grid and depth
    definition are different; that comparison stays hidden entirely -- item 2)."""
    site_dir = data_dir / "teesta"
    run_id, scenario_id = "s003__sph", "s003"
    run_dir = site_dir / "runs" / run_id
    summary = run_dir / "summary_nearfield"
    summary.mkdir(parents=True)
    profile = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32",
               "crs": "EPSG:32645", "transform": from_origin(500000, 3100000, 10, 10),
               "nodata": -9999.0}
    for name in ("max_depth.tif", "max_velocity.tif", "arrival_time.tif"):
        with rasterio.open(summary / name, "w", **profile) as ds:
            ds.write(np.array([[1.0, 0.5], [0.4, 0.0]], dtype="float32"), 1)
    (run_dir / "run_meta.json").write_text(json.dumps(
        {"run_id": run_id, "scenario_id": scenario_id, "thresholds": {"extent_m": 0.3},
         "acceptance": {"status": "ACCEPTED"}, "run_class": "accepted"}))  # synthetic accepted-run fixture
    with registry.connect() as conn:
        conn.execute("INSERT INTO scenarios VALUES (?,?,?,?,?)",
                     (scenario_id, "teesta", "design", json.dumps({}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id,scenario_id,model,status,run_dir,meta_json) "
                     "VALUES (?,?,?,?,?,?)",
                     (run_id, scenario_id, "sph", "postprocessed", str(run_dir),
                      json.dumps({"has_placeholders": False, "placeholder_fields": []})))

    result = real_query.resolve_registered_run("teesta", scenario_id, "sph",
                                                "q_test_0002", {"inputs": {}}, data_dir=data_dir)
    assert result["provenance"]["diagnostics"] == {}
    caveat_ids = {c["id"] for c in result["caveats"]}
    assert "dem_depression_ponding" not in caveat_ids
    assert "clear_water_steep_reach_velocity" not in caveat_ids

"""Feature 13: synthetic D-Flow FM run directories for acceptance / registration tests.

These are tiny, clearly synthetic files shaped like the real kernel's outputs (a 2-face map, a
2-station history, a `.dia`), written under pytest tmp dirs only. They are NOT real results and are
never written to the repository's data/ folder."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.transform import from_origin

EXAMPLE = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "run_meta.example.json"
PROFILE = {"driver": "GTiff", "height": 2, "width": 2, "count": 1, "dtype": "float32",
           "crs": "EPSG:32645", "transform": from_origin(500000, 3100000, 30, 30), "nodata": -9999.0}


def make_run(data_dir: Path, site_id: str = "synth", scenario_id: str = "synth__s001", *,
             poi_dry: bool = False, dia_error: bool = False, outlet_dry: bool = False,
             terrain_mismatch: bool = False, missing_output: str | None = None,
             identity_run_id: str | None = None, with_run_meta: bool = True,
             truncated: bool = False, with_stop_time: bool = True) -> dict:
    run_id = f"{scenario_id}__delft3d"
    site = data_dir / site_id
    run_dir, case = site / "runs" / run_id, site / "runs" / run_id / "case"
    terrain = site / "terrain"
    for d in (run_dir / "summary", case / "output", case / "inputs", terrain, site / "breach" / "hydrographs"):
        d.mkdir(parents=True, exist_ok=True)
    mask_profile = dict(PROFILE)
    if terrain_mismatch:
        mask_profile.update(height=3, width=3)
    with rasterio.open(terrain / "domain_mask.tif", "w", **mask_profile) as ds:
        ds.write(np.ones((mask_profile["height"], mask_profile["width"]), "float32"), 1)
    for name in ("max_depth", "max_velocity", "arrival_time"):
        with rasterio.open(run_dir / "summary" / f"{name}.tif", "w", **PROFILE) as ds:
            ds.write(np.array([[1.0, 0.5], [0.2, 0.0]], "float32"), 1)
    (run_dir / "timeseries.csv").write_text("poi_id,t_s,depth_m,velocity_ms,wse_m,arrival_s_since_t0\na,0,0,0,0,\n")
    (site / "breach" / "hydrographs" / f"{scenario_id}.csv").write_text("t_s,q_m3s\n0,1\n")
    if with_run_meta:
        meta = json.loads(EXAMPLE.read_text())
        meta.update(run_id=identity_run_id or run_id, scenario_id=scenario_id, model="delft3d",
                    status="postprocessed", hydrographs=[f"breach/hydrographs/{scenario_id}.csv"],
                    mass_balance_error_pct=None)
        (run_dir / "run_meta.json").write_text(json.dumps(meta))
        (run_dir / "scenario.json").write_text(json.dumps({"scenario_id": scenario_id, "kind": "design",
                                                          "params": {"water_volume_m3": 1e6}}))
    (case / "model.mdu").write_text("# synthetic\n")
    # Feature 17: completion evidence. Outputs end at t = 60 s; a truncated run was configured to stop
    # at 600 s (killed early, no ** ERROR line) -- simulation_completed must FAIL it.
    if with_stop_time:
        (case / "case_meta.json").write_text(json.dumps({"stop_s": 600.0 if truncated else 60.0,
                                                         "map_interval_s": 60.0, "history_interval_s": 60.0,
                                                         "spinup_s": 0.0}))
    (case / "output" / "model.dia").write_text("** ERROR : synthetic failure\n" if dia_error else "** INFO : done\n")
    # Two 100 m faces; the breach source is at x=0, the outlet polyline on the right edge (x=200).
    (case / "inputs" / "forcing.ext").write_text("[SourceSink]\nxCoordinates = 10.0\nyCoordinates = 50.0\n")
    (case / "inputs" / "downstream_outlet.pli").write_text("out\n2 2\n200.0 0.0\n200.0 100.0\n")
    xr.Dataset({"mesh2d_node_x": ("n", [0.0, 100.0, 200.0, 0.0, 100.0, 200.0]),
                "mesh2d_node_y": ("n", [0.0, 0.0, 0.0, 100.0, 100.0, 100.0]),
                "mesh2d_face_nodes": (("f", "k"), [[0, 1, 4, 3], [1, 2, 5, 4]]),
                "mesh2d_waterdepth": (("time", "f"), [[0.0, 0.0], [0.4, 0.0 if outlet_dry else 0.3]])},
               coords={"time": [0.0, 60.0]}).to_netcdf(case / "output" / "model_map.nc")
    xr.Dataset({"waterdepth": (("time", "s"), [[0.0, 0.0], [0.5, 0.0 if poi_dry else 0.4]]),
                "station_id": ("s", np.array([b"poi_a", b"poi_b"]))},
               coords={"time": [0.0, 60.0]}).to_netcdf(case / "output" / "model_his.nc")
    if missing_output:
        (run_dir / missing_output).unlink()
    return {"run_id": run_id, "scenario_id": scenario_id, "run_dir": run_dir, "case_dir": case,
            "site_dir": site, "terrain_dir": terrain}

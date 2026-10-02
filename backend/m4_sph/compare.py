"""Generic SPH ↔ Delft3D comparison for one scenario (Feature 6).

Pairs `runs/<scenario_id>__delft3d` (Feature 5 D-Flow FM) with `runs/<scenario_id>__sph` (M4) on
the SPH run's declared near-field grid, over the SPH run's own time window (seconds since t0),
using only the runs' real artifacts. Generalises `compare_mvp.build_comparison` (Teesta MVP only,
kept unchanged as the MVP path).

Four statuses are reported separately -- each one BLOCKED / RUNNING / SUCCEEDED / FAILED:

- `solver_status`      both solvers finished successfully
- `postprocess_status` both runs have postprocessed summary artifacts
- `comparison_status`  metrics were computed (BLOCKED by the particle-retention gate)
- `validation_status`  always BLOCKED here: model-to-model agreement is not validation against
                       observations, and no observed dataset is wired to this comparison.

Metrics that cannot be computed from both runs are omitted, never filled in.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

SUCCEEDED, FAILED, BLOCKED, RUNNING = "SUCCEEDED", "FAILED", "BLOCKED", "RUNNING"
EXCLUSION_CAVEAT = "sph_particle_exclusion_warning"


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_state(run_dir: Path, model: str) -> dict:
    """Solver and post-processing state of one run, from its own files."""
    meta = _json(run_dir / "run_meta.json")
    state = {"run_id": run_dir.name, "run_dir": str(run_dir), "exists": run_dir.is_dir()}
    if not run_dir.is_dir():
        return {**state, "solver": BLOCKED, "postprocess": BLOCKED, "reason": "run directory missing"}
    if model == "sph":
        executions = sorted(run_dir.glob("attempts/*/execution.json"))
        last = _json(executions[-1]) if executions else None
        if last is None:
            solver = RUNNING if list(run_dir.glob("attempts/*")) else BLOCKED
        else:
            solver = SUCCEEDED if last.get("success") else FAILED
        state["execution_error"] = (last or {}).get("error")
    else:
        dia = sorted((run_dir / "case" / "output").glob("*.dia"))
        maps = sorted((run_dir / "case" / "output").glob("*_map.nc"))
        if meta and meta.get("status") == "postprocessed":
            solver = SUCCEEDED
        elif dia and any(line.startswith("** ERROR") for line in dia[0].read_text(errors="replace").splitlines()):
            solver = FAILED
        elif dia and not maps:
            solver = RUNNING
        else:
            solver = BLOCKED
    post = SUCCEEDED if meta and meta.get("status") == "postprocessed" else BLOCKED
    return {**state, "solver": solver, "postprocess": post, "run_meta": meta}


def pair_statuses(data_dir: str | Path, site_id: str, scenario_id: str) -> dict:
    runs = Path(data_dir) / site_id / "runs"
    d3d = _run_state(runs / f"{scenario_id}__delft3d", "delft3d")
    sph = _run_state(runs / f"{scenario_id}__sph", "sph")

    def combine(a: str, b: str) -> str:
        for status in (FAILED, RUNNING, BLOCKED):
            if status in (a, b):
                return status
        return SUCCEEDED

    out = {"solver_status": combine(d3d["solver"], sph["solver"]),
           "postprocess_status": combine(d3d["postprocess"], sph["postprocess"]),
           "comparison_status": BLOCKED, "validation_status": BLOCKED,
           "validation_reason": "no observed dataset is paired with this comparison; model-to-model agreement is not validation",
           "runs": {"delft3d": {k: v for k, v in d3d.items() if k != "run_meta"},
                    "sph": {k: v for k, v in sph.items() if k != "run_meta"}}}
    sph_meta = sph.get("run_meta") or {}
    retention = (sph_meta.get("particle_retention") or {}).get("status")
    if out["postprocess_status"] != SUCCEEDED:
        out["comparison_reason"] = "both runs must be postprocessed"
    elif EXCLUSION_CAVEAT in sph_meta.get("caveats", []) or retention == FAILED:
        out["comparison_reason"] = "SPH particle-retention gate: the SPH run lost its fluid (exclusion warning/retention FAILED)"
    else:
        out["comparison_reason"] = None
    return out


def _delft3d_window_on_grid(run_dir: Path, grid, t0: float, t1: float):
    """Window maxima of D-Flow depth/speed rasterised onto `grid` (seconds since t0)."""
    import xarray as xr
    from rasterio.features import rasterize
    from shapely.geometry import Polygon

    from backend.m3_dflowfm.section_discharge import _case_dir, find_map
    from backend.shared.grid import FLOAT_NODATA

    case_dir = _case_dir(run_dir)
    case_meta = _json(case_dir / "case_meta.json") or {}
    spinup = float(case_meta.get("spinup_s", 0.0))
    with xr.open_dataset(find_map(case_dir)) as ds:
        t = np.asarray(ds.time.values)
        t_s = ((t - t[0]) / np.timedelta64(1, "s")).astype(float) if np.issubdtype(t.dtype, np.datetime64) \
            else t.astype(float) - float(t[0])
        t_s = t_s - spinup
        choose = (t_s >= t0) & (t_s <= t1)
        if choose.sum() < 2:
            raise ValueError(f"D-Flow map has fewer than two records in the window [{t0}, {t1}] s since t0")
        idx = np.flatnonzero(choose)
        depth = np.nanmax(np.asarray(ds.mesh2d_waterdepth.isel(time=idx).values, dtype=float), axis=0)
        speed = np.nanmax(np.asarray(ds.mesh2d_ucmag.isel(time=idx).values, dtype=float), axis=0)
        start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
        nodes = np.column_stack((ds.mesh2d_node_x.values, ds.mesh2d_node_y.values))
        polys = []
        for row, dv, sv in zip(ds.mesh2d_face_nodes.values, depth, speed):
            poly = Polygon(nodes[row[np.isfinite(row)].astype(int) - start])
            if poly.is_valid and not poly.is_empty:
                polys.append((poly, float(max(dv, 0.0)), float(max(sv, 0.0))))
    shape, transform = grid.shape, grid.transform
    mask = rasterize(((p, 1) for p, _, _ in polys), out_shape=shape, transform=transform, fill=0, dtype="uint8") > 0
    d = rasterize(((p, v) for p, v, _ in polys), out_shape=shape, transform=transform, fill=FLOAT_NODATA, dtype="float32")
    v = rasterize(((p, s) for p, _, s in polys), out_shape=shape, transform=transform, fill=FLOAT_NODATA, dtype="float32")
    return mask, d, v, t_s[idx]


def pair_metrics(d_mask, d_depth, d_vel, s_depth, s_vel, cell_size_m: float, wet_threshold_m: float,
                 nodata: float) -> tuple[dict, np.ndarray]:
    """IoU / F1 / RMSE / MAE / areas on the common valid footprint (same math as compare_mvp)."""
    valid = d_mask & (d_depth != nodata) & (s_depth != nodata)
    if not valid.any():
        raise ValueError("D-Flow and SPH artifacts have no common valid cells")
    wet_d, wet_s = valid & (d_depth >= wet_threshold_m), valid & (s_depth >= wet_threshold_m)
    union, inter = wet_d | wet_s, wet_d & wet_s
    metrics = {"common_valid_cell_count": int(valid.sum()), "wet_threshold_m": wet_threshold_m}
    if union.any():
        metrics["iou"] = float(inter.sum() / union.sum())
        metrics["f1"] = float(2 * inter.sum() / (wet_d.sum() + wet_s.sum()))
        metrics["depth_rmse_wet_m"] = float(np.sqrt(np.mean((s_depth[union] - d_depth[union]) ** 2)))
        vel = union & (s_vel != nodata) & (d_vel != nodata)
        if vel.any():
            metrics["velocity_mae_ms"] = float(np.mean(np.abs(s_vel[vel] - d_vel[vel])))
    area = cell_size_m ** 2
    metrics["flooded_area_delft3d_m2"] = float(wet_d.sum() * area)
    metrics["flooded_area_sph_m2"] = float(wet_s.sum() * area)
    metrics["flooded_area_difference_m2"] = metrics["flooded_area_sph_m2"] - metrics["flooded_area_delft3d_m2"]
    return metrics, valid


def _arrivals(d3d_dir: Path, sph_dir: Path, t0: float, t1: float, threshold_m: float) -> list[dict]:
    """Arrival-time differences at POIs present in both runs' own time series."""
    import xarray as xr

    from backend.m3_dflowfm.section_discharge import _case_dir

    series = sph_dir / "timeseries.csv"
    his = sorted((_case_dir(d3d_dir) / "output").glob("*_his.nc"))
    if not series.is_file() or len(his) != 1:
        return []
    sph_arrival: dict[str, float] = {}
    for row in csv.DictReader(series.open(newline="", encoding="utf-8")):
        if row.get("depth_m") and float(row["depth_m"]) > threshold_m and row["poi_id"] not in sph_arrival:
            sph_arrival[row["poi_id"]] = float(row["t_s"])
    spinup = float((_json(_case_dir(d3d_dir) / "case_meta.json") or {}).get("spinup_s", 0.0))
    probes = []
    with xr.open_dataset(his[0]) as ds:
        t = np.asarray(ds.time.values)
        ts = (((t - t[0]) / np.timedelta64(1, "s")).astype(float) if np.issubdtype(t.dtype, np.datetime64)
              else t.astype(float) - float(t[0])) - spinup
        names = [x.decode(errors="replace").strip() if isinstance(x, bytes) else str(x).strip()
                 for x in ds.station_id.values]
        depth = np.asarray(ds.waterdepth.values, dtype=float)
    for i, poi in enumerate(names):
        if poi not in sph_arrival:
            continue
        hits = np.flatnonzero((ts >= t0) & (ts <= t1) & (depth[:, i] > threshold_m))
        if hits.size:
            d_arr = float(ts[hits[0]])
            probes.append({"poi_id": poi, "arrival_delft3d_s": d_arr, "arrival_sph_s": sph_arrival[poi],
                           "diff_s": sph_arrival[poi] - d_arr})
    return probes


def build_pair_comparison(data_dir: str | Path, site_id: str, scenario_id: str) -> Path:
    """Write `compare/<scenario_id>/compare.json` (compare.schema.json) with the four statuses."""
    import rasterio

    from backend.shared.grid import FLOAT_NODATA, CanonicalGrid, write_grid_raster

    root = Path(data_dir)
    status = pair_statuses(root, site_id, scenario_id)
    runs = root / site_id / "runs"
    d3d_dir, sph_dir = runs / f"{scenario_id}__delft3d", runs / f"{scenario_id}__sph"
    out = root / site_id / "compare" / scenario_id
    section = {"available": False, "domain": "nearfield", "time_window_s": 0.0, "metrics": {}, "probes": [],
               "layers": [], "run_ids": [r for r, d in ((d3d_dir.name, d3d_dir), (sph_dir.name, sph_dir)) if d.is_dir()]}
    provenance: dict = {"method": "backend.m4_sph.compare.build_pair_comparison"}
    if status["comparison_reason"] is None:
        try:
            sph_meta = _json(sph_dir / "run_meta.json")
            d3d_meta = _json(d3d_dir / "run_meta.json")
            case_meta = _json(sph_dir / "case" / "case_meta.json")
            terrain = root / site_id / "terrain"
            grid = CanonicalGrid.from_json(terrain / "grid_nearfield.json")
            t0, t1 = float(case_meta["t_start_s"]), float(case_meta["t_end_s"])
            d_mask, d_depth, d_vel, used_t = _delft3d_window_on_grid(d3d_dir, grid, t0, t1)
            with rasterio.open(sph_dir / "summary_nearfield" / "max_depth.tif") as ds:
                s_depth = ds.read(1)
                if ds.transform != grid.transform or s_depth.shape != grid.shape:
                    raise ValueError("SPH depth raster is not on the declared near-field grid")
            with rasterio.open(sph_dir / "summary_nearfield" / "max_velocity.tif") as ds:
                s_vel = ds.read(1)
            thr = float((d3d_meta.get("thresholds") or {}).get("extent_m", (sph_meta.get("thresholds") or {}).get("extent_m")))
            metrics, valid = pair_metrics(d_mask, d_depth, d_vel, s_depth, s_vel, grid.cell_size_m, thr, FLOAT_NODATA)
            out.mkdir(parents=True, exist_ok=True)
            diff = np.full(grid.shape, FLOAT_NODATA, dtype=np.float32)
            diff[valid] = s_depth[valid] - d_depth[valid]
            write_grid_raster(out / "depth_diff.tif", diff, grid)
            arrival_thr = float((sph_meta.get("thresholds") or {}).get("arrival_m", thr))
            section.update(available=True, time_window_s=t1 - t0, metrics=metrics,
                           probes=_arrivals(d3d_dir, sph_dir, t0, t1, arrival_thr),
                           layers=[{"layer_id": "depth_diff_nearfield", "type": "raster_png",
                                    "url": f"/api/v1/files/{site_id}/compare/{scenario_id}/depth_diff.png",
                                    "bounds_latlng": grid.bounds_latlng, "style_id": "depth_diff", "unit": "m",
                                    "available": True}])
            status["comparison_status"] = SUCCEEDED
            provenance.update(window_s_since_t0=[t0, t1], delft3d_records_used=int(len(used_t)),
                              comparison_grid=str(terrain / "grid_nearfield.json"),
                              alignment="D-Flow window maxima rasterised onto the SPH near-field grid; common valid footprint")
        except Exception as e:  # reported as FAILED, never hidden or filled in
            status["comparison_status"] = FAILED
            status["comparison_reason"] = f"{type(e).__name__}: {e}"
    section["status"] = status
    result = {"site_id": site_id, "scenario_id": scenario_id, "sph_vs_delft3d": section,
              "emulator_vs_physics": {"available": False, "held_out_run_id": None, "metrics": {}, "layers": []},
              "gp_vs_linear": {}, "when_to_use_key": "when_to_use_sph_delft3d",
              "caveats": [{"id": "direct_solver_output", "severity": "warning", "text_key": "direct_solver_output"}]
                         + ([] if section["available"] else
                            [{"id": "comparison_unavailable", "severity": "warning", "text_key": "comparison_unavailable"}]),
              "provenance": provenance}
    out.mkdir(parents=True, exist_ok=True)
    sidecar = out / "compare.json"
    sidecar.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return sidecar


def status_only_response(data_dir: str | Path, site_id: str, scenario_id: str) -> dict | None:
    """compare.schema.json response carrying only the four statuses, when at least one of the
    pair's runs exists but no comparison sidecar has been built. `None` if neither run exists."""
    status = pair_statuses(data_dir, site_id, scenario_id)
    run_ids = [r["run_id"] for r in status["runs"].values() if r["exists"]]
    if not run_ids:
        return None
    return {"site_id": site_id, "scenario_id": scenario_id,
            "sph_vs_delft3d": {"available": False, "domain": "nearfield", "time_window_s": 0.0, "metrics": {},
                               "probes": [], "layers": [], "run_ids": run_ids, "status": status},
            "emulator_vs_physics": {"available": False, "held_out_run_id": None, "metrics": {}, "layers": []},
            "gp_vs_linear": {}, "when_to_use_key": "comparison_unavailable",
            "caveats": [{"id": "comparison_unavailable", "severity": "warning", "text_key": "comparison_unavailable"}]}


def render_depth_diff(data_dir: str | Path, site_id: str, scenario_id: str) -> bytes:
    from backend.m0_api import rendering

    return rendering.render_and_cache(Path(data_dir) / site_id / "compare" / scenario_id / "depth_diff.tif", "depth_diff")

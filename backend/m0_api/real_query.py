"""Resolve a registered, postprocessed solver run into a query artifact set."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import mapping, shape
from shapely.ops import transform as transform_geometry, unary_union

from datetime import datetime, timezone

from backend.m0_api import run_metadata
from backend.shared.version import version_info
from backend.m0_api import dem_diagnostics, registry
from backend.shared.grid import raster_bounds_latlng


def _estimate(value, unit: str, run_id: str) -> dict:
    return {"value": value, "low": None, "high": None, "unit": unit, "interval": "none",
            "kind": "predicted", "confidence": "LOW", "basis": "direct registered solver output",
            "source": run_id}


def _bounds_latlng(path: Path) -> list[list[float]]:
    """Contract §1.3 `[[south_lat, west_lon], [north_lat, east_lon]]` (docs/progress.md
    2026-09-28 "STEP 2": this used to return `[[west,south],[east,north]]` instead, a real
    axis-order bug every consumer had to compensate for -- fixed at the source via the shared
    `raster_bounds_latlng`, whose axis order is fixed by construction, not guessed)."""
    return raster_bounds_latlng(path)


def _first_arrival(site_dir: Path, run_id: str) -> tuple[str, str, float | None]:
    import csv
    path = site_dir / "runs" / run_id / "timeseries.csv"
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        arrived = [row for row in rows if row.get("arrival_s_since_t0", "") not in ("", "None")]
        if arrived:
            row = min(arrived, key=lambda r: float(r["arrival_s_since_t0"]))
            return row["poi_id"], row.get("name", row["poi_id"]), float(row["arrival_s_since_t0"])
    return "unavailable", "No POI arrival recorded", None


def resolve_registered_run(site_id: str, scenario_id: str, model: str, query_id: str,
                           request: dict, data_dir: str | Path | None = None) -> dict:
    """Build direct-result response and query raster/vector artifacts from a registered run.

    Requires a postprocessed M3/M4 registry row and non-empty, georeferenced summary rasters.
    No example/cache response is used when resolution fails.
    """
    root = Path(data_dir) if data_dir is not None else registry.data_dir()
    conn = registry.connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=? AND scenario_id=? AND model=?",
                           (f"{scenario_id}__{model}", scenario_id, model)).fetchone()
        scenario = conn.execute("SELECT * FROM scenarios WHERE scenario_id=? AND site_id=?",
                                (scenario_id, site_id)).fetchone()
    finally:
        conn.close()
    if row is None or scenario is None or row["status"] not in ("completed", "postprocessed"):
        raise FileNotFoundError(f"no completed registered {model} run for {site_id}/{scenario_id}")
    site_dir = root / site_id
    run_dir = Path(row["run_dir"])
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    run_meta_path = run_dir / "run_meta.json"
    if not run_meta_path.is_file() or run_meta_path.stat().st_size == 0:
        raise FileNotFoundError(f"registered run has no nonempty run_meta.json: {run_meta_path}")
    run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
    if run_meta.get("run_id") != row["run_id"] or run_meta.get("scenario_id") != scenario_id:
        raise ValueError("run_meta.json identity does not match the registered run/scenario")
    extent_threshold_m = run_meta.get("thresholds", {}).get("extent_m")
    if not isinstance(extent_threshold_m, (int, float)) or extent_threshold_m < 0:
        raise ValueError("run_meta.json does not contain a valid thresholds.extent_m")
    summary_dir = run_dir / ("summary_nearfield" if model == "sph" else "summary")
    sources = {"depth_p50": summary_dir / "max_depth.tif",
               "velocity_p50": summary_dir / "max_velocity.tif",
               "arrival_p50": summary_dir / "arrival_time.tif"}
    for layer_id, path in sources.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"registered run is missing nonempty {layer_id} artifact: {path}")
    with rasterio.open(sources["depth_p50"]) as ds:
        depth = ds.read(1).astype(np.float64)
        valid = np.isfinite(depth)
        if ds.nodata is not None:
            valid &= depth != ds.nodata
        if not valid.any():
            raise ValueError("registered depth raster contains no valid cells")
        pixel_area = abs(ds.transform.a * ds.transform.e - ds.transform.b * ds.transform.d)
        inundated = valid & (depth >= extent_threshold_m)
        area = float(inundated.sum() * pixel_area)
        max_depth = float(np.max(depth[valid]))
        bounds = _bounds_latlng(sources["depth_p50"])
        depth_signature = (ds.shape, ds.crs, ds.transform)
    with rasterio.open(sources["velocity_p50"]) as ds:
        if (ds.shape, ds.crs, ds.transform) != depth_signature:
            raise ValueError("depth and velocity rasters do not share dimensions/georeferencing")
        velocity = ds.read(1).astype(float)
        vvalid = np.isfinite(velocity) & ((ds.nodata is None) | (velocity != ds.nodata))
        max_velocity = float(np.max(velocity[vvalid])) if vvalid.any() else None

    extra_caveats: list[dict] = []
    diagnostics: dict = {}
    if model == "delft3d":
        # The DEM matching this run's mesh (config/registered_runs.yaml `terrain_site_id`, e.g. the
        # Teesta MVP run's frozen teesta_pilot 90 m DEM; default: the site's own terrain).
        dem_path = run_metadata.terrain_dir(site_dir, row["run_id"], run_meta) / "dem.tif"
        if dem_path.is_file():
            settings = dem_diagnostics.load_settings()
            cache_dir = site_dir / "_diagnostics"
            depression_path, slope_path = dem_diagnostics.ensure_cached_diagnostics(dem_path, cache_dir)
            with rasterio.open(sources["depth_p50"]) as ds:
                like_transform, like_crs, like_shape = ds.transform, ds.crs, ds.shape
            depression_depth = dem_diagnostics.resample_to(
                depression_path, like_transform=like_transform, like_crs=like_crs, like_shape=like_shape)
            slope = dem_diagnostics.resample_to(
                slope_path, like_transform=like_transform, like_crs=like_crs, like_shape=like_shape)
            pit_mask = inundated & (depression_depth >= settings.dem_depression_depth_threshold_m)
            steep_mask = inundated & (slope >= settings.steep_reach_slope_threshold)
            wet_count = int(inundated.sum())
            max_depth_idx = np.unravel_index(np.nanargmax(np.where(valid, depth, -np.inf)), depth.shape)
            max_depth_in_pit = bool(pit_mask[max_depth_idx])
            diagnostics["dem_pit_fraction_of_wet_area"] = (
                float(pit_mask.sum()) / wet_count if wet_count else 0.0)
            diagnostics["max_depth_cell_in_dem_pit"] = max_depth_in_pit
            if max_depth_in_pit or diagnostics["dem_pit_fraction_of_wet_area"] > 0.0:
                extra_caveats.append({"id": "dem_depression_ponding", "severity": "warning",
                                      "text_key": "caveat_dem_depression_ponding"})
            if vvalid.any():
                max_vel_idx = np.unravel_index(np.nanargmax(np.where(vvalid, velocity, -np.inf)),
                                                velocity.shape)
                max_vel_on_steep = bool(steep_mask[max_vel_idx])
                diagnostics["max_velocity_cell_on_steep_reach"] = max_vel_on_steep
                diagnostics["steep_reach_fraction_of_wet_area"] = (
                    float(steep_mask.sum()) / wet_count if wet_count else 0.0)
                if max_vel_on_steep:
                    extra_caveats.append({"id": "clear_water_steep_reach_velocity", "severity": "warning",
                                          "text_key": "caveat_clear_water_steep_reach_velocity"})

    query_dir = site_dir / "queries" / query_id
    layers_dir = query_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    style_units = {"depth_p50": "m", "velocity_p50": "m/s", "arrival_p50": "s"}
    for layer_id, source in sources.items():
        destination = layers_dir / f"{layer_id}.tif"
        shutil.copyfile(source, destination)
        layers.append({"layer_id": layer_id, "label_key": f"layer_{layer_id}", "type": "raster_png",
                       "url": f"/api/v1/flood/{query_id}/layers/{layer_id}.png", "bounds_latlng": bounds,
                       "style_id": layer_id, "unit": style_units[layer_id], "available": True,
                       "source_run_id": row["run_id"]})

    # Vector extent is polygonized from the actual depth output. A direct deterministic run has
    # no emulator probability/uncertainty, so it must not be styled as HIGH/POSSIBLE classes.
    with rasterio.open(sources["depth_p50"]) as ds:
        nodata = ds.nodata if ds.nodata is not None else -9999.0
        extent_mask = valid & (depth >= extent_threshold_m)
        features = [shape(geom) for geom, val in shapes(extent_mask.astype(np.uint8), mask=extent_mask,
                                                          transform=ds.transform) if val == 1]
        union = unary_union(features) if features else None
        if union is None or union.is_empty:
            geojson = {"type": "FeatureCollection", "features": []}
        else:
            to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
            geom_wgs84 = transform_geometry(to_wgs84, union)
            geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": mapping(geom_wgs84),
                         "properties": {"source_run_id": row["run_id"], "synthetic": False}}]}
    (query_dir / "extent.geojson").write_text(json.dumps(geojson), encoding="utf-8")
    params = json.loads(scenario["params_json"])
    for name, requested in request.get("inputs", {}).items():
        if not isinstance(requested, dict) or requested.get("type") != "exact" or name not in params:
            raise ValueError(f"direct run input {name!r} must exactly match a stored scenario parameter")
        if float(requested["value"]) != float(params[name]):
            raise ValueError(f"direct run input {name!r} does not match registered scenario {scenario_id!r}")
    resolved_inputs = {k: {"value": v, "low": v, "high": v, "unit": None, "interval": "none",
                           "kind": "input", "confidence": None} for k, v in params.items()
                       if isinstance(v, (int, float))}
    poi_id, poi_name, arrival_s = _first_arrival(site_dir, row["run_id"])
    confidence = {"level": "LOW", "components": {"validation_skill": "UNKNOWN",
                   "query_coverage": "INSIDE"}, "reason_key": "conf_direct_solver"}
    result = {
        "contract_version": "0.3.0", "query_id": query_id, "site_id": site_id,
        "status": "complete", "method": f"{model}_direct", "mode": "scenario",
        "resolved_inputs": resolved_inputs,
        "summary": {"inundated_area_m2": _estimate(area, "m2", row["run_id"]),
                    "max_depth_m": _estimate(max_depth, "m", row["run_id"]),
                    "max_velocity_ms": _estimate(max_velocity, "m/s", row["run_id"]),
                    "peak_discharge_m3s": {"value": None, "low": None, "high": None, "unit": "m3/s",
                        "interval": "none", "kind": "predicted", "confidence": "LOW",
                        "basis": "not emitted by registered M3/M4 summary artifacts", "source": row["run_id"]},
                    "first_arrival": {"poi_id": poi_id, "name": poi_name,
                                      "arrival_s": _estimate(arrival_s, "s", row["run_id"])}},
        "confidence": {key: confidence for key in ("overall", "extent", "depth", "arrival", "velocity")},
        "layers": layers, "vectors": {"extent_url": f"/api/v1/flood/{query_id}/extent.geojson"},
        "flags": {"outside_trained_range": False, "demo_mode": False, "library_outdated": False,
                  "has_placeholders": bool(json.loads(row["meta_json"] or "{}").get("has_placeholders", False))},
        "placeholder_fields": json.loads(row["meta_json"] or "{}").get("placeholder_fields", []),
        "caveats": [{"id": "direct_solver_output", "severity": "warning",
                     "text_key": "caveat_direct_solver_output"}, *extra_caveats],
        "provenance": {"method": f"{model}_direct", "contract_version": "0.3.0",
                       "run_ids": [row["run_id"]], "parameters": params,
                       "output_classification": run_meta.get("output_classification", "REAL_SIMULATION_ARTIFACT"),
                       "domain_status": run_meta.get("domain_status"),
                       "input_forcing_status": run_meta.get("input_forcing_status"),
                       "scientific_claim": run_meta.get("scientific_claim"),
                       "input_forcing_note": run_metadata.input_forcing_note(row["run_id"], run_meta),
                       "solver": run_meta.get("solver") or model,
                       "solver_version": run_meta.get("solver_version") or run_meta.get("kernel_version"),
                       "run_completed_at": run_meta.get("completed_at") or run_meta.get("finished_at"),
                       "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       **version_info(),
                       "data_sources": [str(run_meta_path.relative_to(root))],
                       "diagnostics": diagnostics},
        "timing_ms": {"median_phase": 0.0, "full_phase": 0.0},
    }
    result_path = query_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result

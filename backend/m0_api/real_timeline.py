"""Build sparse timeline snapshots from a registered D-Flow FM map file.

Each saved frame is an exact solver output record rasterized on the same grid
as the run's canonical summary rasters. This is not an interpolated flood
front or an uncertainty ensemble.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.features import rasterize

from backend.m0_api import run_metadata
from backend.m3_common.postprocess import _faces, _rasterize, _time_seconds, _write_raster
from backend.shared.grid import raster_bounds_latlng


def _provenance_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd()))
    except ValueError:
        return str(resolved)


def create_timeline(site_dir: Path, query_dir: Path, run_id: str) -> Path:
    run_dir = site_dir / "runs" / run_id
    meta = json.loads((run_dir / "run_meta.json").read_text())
    case_dir = run_dir / meta.get("case_dir", "case")
    output_dir = case_dir / "output"
    map_path = next(output_dir.glob("*_map.nc"), None)
    if map_path is None:
        raise FileNotFoundError(f"registered run has no FM map file under {output_dir}")
    summary = run_dir / "summary" / "max_depth.tif"
    if not summary.is_file() or not summary.stat().st_size:
        raise FileNotFoundError(f"registered run lacks canonical depth summary {summary}")

    with rasterio.open(summary) as ds:
        grid = {"height": ds.height, "width": ds.width,
                "origin_x": ds.transform.c, "origin_y": ds.transform.f,
                "cell_size_m": ds.transform.a, "nodata": ds.nodata or -9999.0,
                "crs_epsg": ds.crs.to_epsg()}
        transform = ds.transform
        domain_mask = ds.read(1) != ds.nodata
        # Contract §1.3 [[south,west],[north,east]] -- see docs/progress.md 2026-09-28 "STEP 2":
        # this used to be a hand-rolled [[west,south],[east,north]], a real axis-order bug.
        bounds_latlng = raster_bounds_latlng(ds)

    timeline_dir = query_dir / "timeline"
    timeline_dir.mkdir(parents=True, exist_ok=True)
    with xr.open_dataset(map_path) as ds:
        if "mesh2d_waterdepth" not in ds or "time" not in ds:
            raise ValueError(f"FM map has no water-depth time series: {map_path}")
        times = _time_seconds(ds.time.values)
        face_polygons = _faces(ds)
        # Select actual records spanning the run's observed flooded-cell-count
        # progression. Duplicate threshold crossings are collapsed.
        counts = np.zeros(len(times), dtype=np.int64)
        for i in range(len(times)):
            depth = np.asarray(ds.mesh2d_waterdepth.isel(time=i).values)
            counts[i] = np.count_nonzero(np.isfinite(depth) & (depth >= float(meta["thresholds"]["extent_m"])))
        peak = int(counts.max())
        if peak <= 0:
            raise ValueError("FM map contains no wet records at the run extent threshold")
        selected = [int(np.flatnonzero(counts >= peak * fraction)[0]) for fraction in (0.05, 0.35, 0.75)]
        selected.append(int(counts.argmax()))
        selected.append(len(times) - 1)
        selected = list(dict.fromkeys(selected))
        frame_rows = []
        for index in selected:
            depth = np.asarray(ds.mesh2d_waterdepth.isel(time=index).values, dtype=np.float32)
            frame = _rasterize(depth, face_polygons, grid, transform, domain_mask)
            t_s = int(round(float(times[index])))
            filename = f"depth_t{t_s}.tif"
            frame_path = timeline_dir / filename
            _write_raster(frame_path, frame, grid, transform)
            with rasterio.open(frame_path, "r+") as frame_ds:
                frame_ds.update_tags(source_classification="REAL_SIMULATION_ARTIFACT",
                    source_run_id=run_id, source_map_file=str(map_path.resolve()),
                    source_time_s=str(t_s), frame_method="exact D-Flow FM output record rasterized to run summary grid")
            valid = (frame != grid["nodata"]) & np.isfinite(frame)
            frame_rows.append({"t_s": t_s, "file": filename,
                               "wet_cells": int(np.count_nonzero(valid & (frame >= meta["thresholds"]["extent_m"]))),
                               "max_depth_m": float(frame[valid].max()) if valid.any() else 0.0})

    # Reuse the exact forcing delivered to the solver when available; values
    # remain explicitly reconstructed input forcing, not an observed series.
    forcing_candidates = run_metadata.forcing_candidates(site_dir, case_dir, run_id)
    forcing_path = next((p for p in forcing_candidates if p.is_file()), None)
    hydrographs = []
    if forcing_path:
        with forcing_path.open(newline="", encoding="utf-8") as stream:
            points = [{"t_s": float(r["t_s_since_hydrograph_start"]), "q_m3s": float(r["q_m3s"])}
                      for r in csv.DictReader(stream)]
        hydrographs = [{"dam_id": run_metadata.dam_id(site_dir.name, run_id), "t_offset_s": 0.0, "points": points}]

    # POI arrival rows come from M3's processed history time series.
    profile, pois = [], []
    poi_path = run_metadata.terrain_dir(site_dir, run_id) / "pois.gpkg"
    timeseries = run_dir / "timeseries.csv"
    if poi_path.is_file() and timeseries.is_file():
        import geopandas as gpd
        poi_frame = gpd.read_file(poi_path)
        records: dict[str, list[dict]] = {}
        with timeseries.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                records.setdefault(row["poi_id"], []).append(row)
        for poi in poi_frame.to_dict("records"):
            poi_id = poi["poi_id"]
            pois.append({"poi_id": poi_id, "name": poi["name"], "chainage_m": float(poi["chainage_m"])})
            rows = records.get(poi_id, [])
            arrival = next((float(r["arrival_s_since_t0"]) for r in rows if r.get("arrival_s_since_t0")), None)
            if arrival is not None:
                profile.append({"chainage_m": float(poi["chainage_m"]), "arrival_p10_s": None,
                                "arrival_p50_s": arrival, "arrival_p90_s": None})

    provenance_paths = [_provenance_path(map_path)]
    for source_path in (forcing_path, poi_path, timeseries):
        if source_path is not None and source_path.is_file():
            provenance_paths.append(_provenance_path(source_path))
            sidecar = source_path.with_suffix(".provenance.json")
            if sidecar.is_file():
                provenance_paths.append(_provenance_path(sidecar))
    data = {"mode": "delft3d_snapshots", "t_end_s": float(times[-1]), "frames": frame_rows,
            "bounds_latlng": bounds_latlng, "hydrographs": hydrographs,
            "arrival_profile": profile, "pois_on_profile": pois,
            "caveats": [
                {"id": "timeline_single_solver", "severity": "warning", "text_key": "timeline_single_solver"},
                {"id": "mvp_reconstructed_forcing", "severity": "warning", "text_key": "mvp_reconstructed_forcing"},
            ],
            "provenance": {"method": "delft3d_direct", "contract_version": "0.3.0",
                           "timeline_method": "delft3d_map_snapshots", "run_ids": [run_id],
                           "data_sources": provenance_paths,
                           "output_classification": "REAL_SIMULATION_ARTIFACT",
                           "input_forcing_status": meta.get("input_forcing_status"),
                           "scientific_claim": meta.get("scientific_claim"),
                           "snapshot_interval_s": float(np.median(np.diff(times))) if len(times) > 1 else None,
                           "selected_records": frame_rows}}
    (timeline_dir / "timeline_data.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return timeline_dir

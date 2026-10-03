"""Reproducible, explicitly limited Teesta MVP SPH comparison preparation.

This adapter uses the actual Teesta M3 map fields and M1 terrain. It does not
claim a production routing section: D-Flow's retained map has depth and speed
magnitude but no velocity vector, so section-normal speed is an MVP assumption.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import xarray as xr
import yaml
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon

from backend.m1_terrain.stl import build_triangles, write_nearfield_frame, write_stl
from backend.m3_dflowfm.routed_discharge import write_routed_discharge
from backend.shared.grid import CanonicalGrid, FLOAT_NODATA, write_grid_raster

SITE_ID = "teesta"
SCENARIO_ID = "teesta_2023_mvp"
M3_RUN_ID = "teesta_2023_mvp__delft3d"
M4_RUN_ID = "teesta_2023_mvp__dualsphysics"
DOMAIN_STATUS = "MVP_NEAR_FIELD"
SECTION_ID = "MVP_COMPARISON_SECTION"
SECTION_WIDTH_M = 300.0
DOMAIN_WIDTH_M = 600.0
TIME_WINDOW_S = 600.0
M3_PEAK_SEARCH_S = (22_000.0, 24_000.0)


def _face_polygons(ds: xr.Dataset) -> list[Polygon]:
    nodes = np.column_stack((ds.mesh2d_node_x.values, ds.mesh2d_node_y.values))
    polygons = []
    for row in ds.mesh2d_face_nodes.values:
        ids = row[np.isfinite(row)].astype(int) - 1  # UGRID connectivity is one-based.
        polygons.append(Polygon(nodes[ids]))
    return polygons


def extract_mvp_section(run_dir: str | Path, terrain_dir: str | Path,
                        output_dir: str | Path) -> tuple[Path, Path, dict]:
    """Integrate actual M3 depth and speed magnitude along a 300 m POI-normal section."""
    run_dir, terrain_dir = Path(run_dir), Path(terrain_dir)
    meta = json.loads((run_dir / "run_meta.json").read_text())
    if meta.get("run_id") != M3_RUN_ID or meta.get("solver_status") != "REAL_SOLVER_OUTPUT":
        raise ValueError("MVP section extraction requires the registered real Teesta D-Flow run")
    map_path = run_dir / meta["case_dir"] / "output" / "teesta_pilot_s001__dflowfm_map.nc"
    his_path = run_dir / meta["case_dir"] / "output" / "teesta_pilot_s001__dflowfm_his.nc"
    if not map_path.is_file() or not his_path.is_file():
        raise FileNotFoundError("real D-Flow map/history artifact missing")
    poi = gpd.read_file(terrain_dir / "pois.gpkg")
    poi = poi.loc[poi.poi_id == "teesta__poi__chungthang"]
    if len(poi) != 1:
        raise ValueError("expected exactly one retained Chungthang POI")
    centreline = gpd.read_file(terrain_dir / "centreline.gpkg").geometry.iloc[0]
    centre = centreline.interpolate(centreline.project(poi.geometry.iloc[0]))
    s = centreline.project(centre)
    p0, p1 = centreline.interpolate(max(0.0, s - 100.0)), centreline.interpolate(min(centreline.length, s + 100.0))
    tx, ty = p1.x - p0.x, p1.y - p0.y
    norm = math.hypot(tx, ty)
    if norm == 0:
        raise ValueError("cannot derive section normal from degenerate centreline")
    tx, ty = tx / norm, ty / norm
    half = SECTION_WIDTH_M / 2
    section = LineString([(centre.x - ty * half, centre.y + tx * half),
                          (centre.x + ty * half, centre.y - tx * half)])

    with xr.open_dataset(map_path) as ds:
        polygons = _face_polygons(ds)
        ids = [i for i, poly in enumerate(polygons) if poly.intersects(section)]
        if not ids:
            raise ValueError("MVP comparison section does not intersect D-Flow mesh")
        lengths = np.asarray([polygons[i].intersection(section).length for i in ids])
        t_s = (ds.time.values - ds.time.values[0]) / np.timedelta64(1, "s")
        depth = ds.mesh2d_waterdepth[:, ids].values
        speed = ds.mesh2d_ucmag[:, ids].values
        q = np.nansum(np.maximum(depth, 0.0) * np.maximum(speed, 0.0) * lengths[None, :], axis=1)
        peak_window = (t_s >= M3_PEAK_SEARCH_S[0]) & (t_s <= M3_PEAK_SEARCH_S[1])
        if not peak_window.any():
            raise ValueError("D-Flow map has no samples in the Chungthang peak-search interval")
        with xr.open_dataset(his_path) as his:
            names = [n.decode(errors="replace") if isinstance(n, bytes) else str(n) for n in his.station_name.values]
            station_ids = [i for i, n in enumerate(names) if "poi__chungthang" in n]
            if len(station_ids) != 1:
                raise ValueError("D-Flow history has no unique Chungthang station")
            htime = (his.time.values - his.time.values[0]) / np.timedelta64(1, "s")
            station_depth = his.waterdepth[:, station_ids[0]].values
            history_window = (htime >= M3_PEAK_SEARCH_S[0]) & (htime <= M3_PEAK_SEARCH_S[1])
            peak_idx = np.where(history_window)[0][np.argmax(station_depth[history_window])]
            peak_time_s = float(htime[peak_idx])

        t0, t1 = peak_time_s - TIME_WINDOW_S, peak_time_s
        samples = (t_s > t0) & (t_s < t1)
        if samples.sum() < 2:
            raise ValueError("D-Flow map lacks at least two samples in the selected 600 s window")
        selected_t = np.concatenate(([t0], t_s[samples], [t1]))
        selected_q = np.interp(selected_t, t_s, q)
        local_t = selected_t - t0
        local_q = selected_q
        peak_idx = np.where(peak_window)[0][np.argmax(q[peak_window])]
        active_intervals = []
        for face_id in ids:
            if ds.mesh2d_waterdepth.values[peak_idx, face_id] <= 0.3:
                continue
            crossing = polygons[face_id].intersection(section)
            segments = ([crossing] if crossing.geom_type == "LineString" else
                        list(crossing.geoms) if crossing.geom_type == "MultiLineString" else [])
            for segment in segments:
                endpoints = [section.project(Point(segment.coords[0])), section.project(Point(segment.coords[-1]))]
                active_intervals.append((min(endpoints), max(endpoints)))
        if not active_intervals:
            raise ValueError("no D-Flow wet faces at the MVP section peak; cannot define an inlet aperture")
        active_start, active_end = min(x[0] for x in active_intervals), max(x[1] for x in active_intervals)
        inlet_width = active_end - active_start
        if inlet_width <= 2 * 10.0:
            raise ValueError(f"D-Flow wet section ({inlet_width:.1f} m) is too narrow for the 10 m terrain grid")
        inlet_centre = section.interpolate((active_start + active_end) / 2)
        # Inlet height is set from M3's measured maximum depth across the section/window.
        section_depth = float(np.nanmax(depth[samples]))

    output_dir = Path(output_dir)
    csv_path, manifest = write_routed_discharge(
        output_dir, site_id=SITE_ID, scenario_id=SCENARIO_ID, source_run_id=M3_RUN_ID,
        t_s=local_t, q_m3s=local_q,
        routing_method="MVP depth × speed-magnitude line integration (normal-speed assumption)",
        section={"type": "LineString", "coordinates": [list(xy) for xy in section.coords], "crs": "EPSG:32645",
                 "id": SECTION_ID, "width_m": SECTION_WIDTH_M,
                 "purpose": "MVP comparison only; not a production routing section"},
        provenance={"status": "REAL_M3_DERIVED_MVP_APPROXIMATION",
                    "source_map": str(map_path), "source_history": str(his_path),
                    "depth_variable": "mesh2d_waterdepth", "speed_variable": "mesh2d_ucmag",
                    "velocity_vector_available": False,
                    "normal_velocity_assumption": "speed magnitude treated as section-normal speed",
                    "time_window_source_s": [t0, t1], "m3_peak_station": "teesta_pilot__poi__chungthang",
                    "m3_chungthang_peak_depth_time_s": peak_time_s,
                    "section_id": SECTION_ID},
        # Feature 17: MVP-only artifact (never production-valid); speed magnitude is treated as
        # section-normal velocity, so Q is an upper bound where flow crosses the section obliquely.
        caveats=["mvp_comparison_only", "speed_magnitude_upper_bound"],
    )
    (output_dir / "m4_inlet.json").write_text(json.dumps({
        "site_id": SITE_ID, "scenario_id": SCENARIO_ID, "source_m3_run_id": M3_RUN_ID,
        "inlet_height_m": section_depth, "time_window_s": TIME_WINDOW_S,
        "time_window_source_s": [t0, t1], "section_id": SECTION_ID,
        "status": "MVP_DERIVED_FROM_REAL_M3", "caveat": "not production routing; speed magnitude treated as normal velocity",
    }, indent=2) + "\n")
    return csv_path, manifest, {"section": section, "centre": centre, "inlet_centre": inlet_centre,
                                "active_start_m": active_start, "active_end_m": active_end,
                                "inlet_width_m": inlet_width, "section_peak_time_s": float(t_s[peak_idx]),
                                "t0_s": t0, "t1_s": t1, "inlet_height_m": section_depth}


def prepare_mvp_terrain(source_terrain_dir: str | Path, output_terrain_dir: str | Path,
                        sites_dir: str | Path, section_meta: dict | None = None) -> dict:
    """Crop the real 10 m Teesta DEM to a reproducible 600 m square around Chungthang."""
    src, out = Path(source_terrain_dir), Path(output_terrain_dir)
    out.mkdir(parents=True, exist_ok=True)
    pois = gpd.read_file(src / "pois.gpkg")
    point = pois.loc[pois.poi_id == "teesta__poi__chungthang"].geometry.iloc[0]
    centreline_gdf = gpd.read_file(src / "centreline.gpkg")
    centreline = centreline_gdf.geometry.iloc[0]
    centre = centreline.interpolate(centreline.project(point))
    cs = 10.0
    left = math.floor((centre.x - DOMAIN_WIDTH_M / 2) / cs) * cs
    right = left + DOMAIN_WIDTH_M
    top = math.ceil((centre.y + DOMAIN_WIDTH_M / 2) / cs) * cs
    bottom = top - DOMAIN_WIDTH_M
    with rasterio.open(src / "dem_nearfield.tif") as ds:
        if not (ds.bounds.left <= left and ds.bounds.right >= right and ds.bounds.bottom <= bottom and ds.bounds.top >= top):
            raise ValueError("MVP crop extends beyond the existing real near-field DEM")
        window = rasterio.windows.from_bounds(left, bottom, right, top, transform=ds.transform)
        dem = ds.read(1, window=window)
    if dem.shape != (int(DOMAIN_WIDTH_M / cs), int(DOMAIN_WIDTH_M / cs)):
        raise ValueError(f"unexpected crop dimensions {dem.shape}")
    if np.all(dem == FLOAT_NODATA):
        raise ValueError("MVP crop contains no valid terrain")
    grid = CanonicalGrid(site_id=SITE_ID, grid_id="nearfield", crs_epsg=32645,
                         origin_x=left, origin_y=top, cell_size_m=cs,
                         width=dem.shape[1], height=dem.shape[0])
    grid.to_json(out / "grid_nearfield.json")
    write_grid_raster(out / "dem_nearfield.tif", dem.astype(np.float32), grid)
    frame = {"contract_version": "0.3.0", "crs_epsg": 32645,
             "origin_x": left, "origin_y": bottom, "units": "m"}
    (out / "nearfield_frame.json").write_text(json.dumps(frame, indent=2) + "\n")
    triangles = build_triangles(dem, grid, frame)
    aperture = None
    if section_meta is not None:
        section = section_meta["section"]
        # Keep an opening in the real terrain surface only across the M3-wet part of
        # the inlet section. This is the boundary aperture needed to avoid overlapping
        # terrain boundary particles and the DualSPHysics in/out particles.
        start = max(0.0, section_meta["active_start_m"] - cs)
        end = min(section.length, section_meta["active_end_m"] + cs)
        aperture = LineString([section.interpolate(start), section.interpolate(end)])
        centres_xy = triangles[:, :, :2].mean(axis=1)
        xy_utm = centres_xy + np.array([frame["origin_x"], frame["origin_y"]])
        ax, ay = aperture.coords[0]
        bx, by = aperture.coords[-1]
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        fraction = np.clip(((xy_utm[:, 0] - ax) * dx + (xy_utm[:, 1] - ay) * dy) / denom, 0.0, 1.0)
        distance = np.hypot(xy_utm[:, 0] - (ax + fraction * dx), xy_utm[:, 1] - (ay + fraction * dy))
        triangles = triangles[distance > cs * 0.75]
    write_stl(triangles, out / "nearfield.stl", header=b"Teesta MVP crop from M1 DEM")
    shutil.copy2(src / "centreline.gpkg", out / "centreline.gpkg")
    shutil.copy2(src / "pois.gpkg", out / "pois.gpkg")

    inlet_point = section_meta["inlet_centre"] if section_meta is not None else centre
    x, y = inlet_point.x, inlet_point.y
    lon, lat = Transformer.from_crs(32645, 4326, always_xy=True).transform(x, y)
    config_path = Path(sites_dir) / f"{SITE_ID}.yaml"
    site_cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "sites" / f"{SITE_ID}.yaml").read_text())
    loc = site_cfg["domains"]["near_field"]["inflow"]["location"]
    loc["value"] = [float(lon), float(lat)]
    loc["note"] = "MVP inlet location derived from retained Chungthang POI snapped to M1 centreline; not a surveyed production inlet."
    Path(sites_dir).mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(site_cfg, sort_keys=False))
    provenance = {
        "label": "TEESTA MVP — NEAR-FIELD SPH COMPARISON", "domain_status": DOMAIN_STATUS,
        "source_terrain": str(src / "dem_nearfield.tif"), "source_crs": "EPSG:32645",
        "target_crs": "EPSG:32645", "extent_utm_m": [left, bottom, right, top],
        "resolution_m": cs, "transform": "axis-aligned 600 m crop, no reprojection/resampling",
        "crop_center_source": "teesta__poi__chungthang snapped to centreline.gpkg",
        "dem_valid_cells": int(np.count_nonzero(dem != FLOAT_NODATA)),
        "dem_min_m": float(np.min(dem[dem != FLOAT_NODATA])),
        "dem_max_m": float(np.max(dem[dem != FLOAT_NODATA])),
        "inlet_aperture": (None if aperture is None else {
            "section_id": SECTION_ID, "width_m": float(aperture.length),
            "opening_method": "remove terrain STL triangles within 7.5 m of the M3-wet section segment plus 10 m ends",
            "source": "actual D-Flow faces with depth > 0.3 m at section discharge peak"}),
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return {**provenance, "centre_xy": [x, y], "inlet_lonlat": [lon, lat],
            "terrain_dir": str(out), "sites_dir": str(sites_dir)}


def prepare_and_register(data_dir: str | Path, binaries_dir: str | Path,
                         source_run_dir: str | Path, source_terrain_dir: str | Path,
                         retry_failed: bool = False, retry_known_bad: bool = False) -> str:
    """Create one M4 case and register it with the existing M0 campaign worker.

    `retry_known_bad` permits retrying a run that finished (`status` isn't `"failed"`) but is
    marked bad by its own `sph_particle_exclusion_warning` caveat -- distinct from `retry_failed`,
    which is for a run the worker itself marked `"failed"`. Used once, for the outlet-zone fix
    (`docs/decisions.md` "M4: SPH outlet zone"): a session-scoped budget of 2 further solver
    attempts (attempts 3 and 4) to verify the fix against the known-bad a02 result, per an explicit
    instruction to diagnose first and ask before a third.
    """
    from backend.m0_api import jobs, registry
    from backend.m4_sph import generator
    from backend.m4_sph.settings import SphSettings

    data_dir = Path(data_dir).resolve()
    artifacts = data_dir / SITE_ID / "mvp_sph"
    terrain_dir = artifacts / "terrain"
    sites_dir = artifacts / "sites"
    run_dir = data_dir / SITE_ID / "runs" / M4_RUN_ID
    registry.init_db()
    check_conn = registry.connect()
    try:
        registered = check_conn.execute("SELECT status, meta_json FROM runs WHERE run_id=?", (M4_RUN_ID,)).fetchone()
    finally:
        check_conn.close()
    attempt = 0
    registered_meta = json.loads(registered["meta_json"] or "{}") if registered else {}
    is_known_bad = "sph_particle_exclusion_warning" in registered_meta.get("caveats", [])
    permitted_retry = (registered["status"] == "failed" and retry_failed) or (is_known_bad and retry_known_bad) if registered else False
    if registered and not permitted_retry:
        raise FileExistsError(f"refusing to overwrite registered M4 run: {M4_RUN_ID}")
    if registered:
        attempt = int(registered_meta.get("attempt", 0)) + 1
        # Attempts 0 and 1 were both run with the same erroneous inlet orientation; attempt 2
        # (a02) verified the corrected orientation but surfaced a separate, still-unfixed defect
        # (sph_particle_exclusion_warning). Attempts 3-4 are this session's outlet-zone-fix budget
        # (see docstring); attempt 5 is terminal without explicit authorization.
        if attempt > 4:
            raise RuntimeError(
                "the M4 outlet-zone-fix verification budget (attempts 3-4) has already been used; "
                "a further attempt needs explicit authorization"
            )
        from backend.m4_sph.launcher import archive_failed_output
        archive_failed_output(data_dir / SITE_ID / "runs" / M4_RUN_ID, attempt - 1)
    route_csv, route_json, section_meta = extract_mvp_section(source_run_dir, source_terrain_dir,
                                                               artifacts / "routed_discharge")
    terrain_meta = prepare_mvp_terrain(source_terrain_dir, terrain_dir, sites_dir, section_meta)
    settings = SphSettings(dp_m=1.5, t_end_s=TIME_WINDOW_S, time_out_s=5.0,
                           inlet_width_m=section_meta["inlet_width_m"],
                           inlet_height_m=section_meta["inlet_height_m"],
                           surface_interval_s=30.0, vram_budget_mib=8188.0, vram_margin=0.15,
                           binaries_dir=str(binaries_dir))
    spec, case_meta = generator.build_nearfield_case(
        SITE_ID, SCENARIO_ID, {}, settings=settings, data_dir=data_dir, sites_dir=sites_dir,
        routed_discharge_path=route_json, terrain_dir=terrain_dir,
    )
    case_meta.update({"run_id": M4_RUN_ID, "label": "TEESTA MVP — NEAR-FIELD SPH COMPARISON",
                      "domain_status": DOMAIN_STATUS, "output_classification": "MVP_CASE_INPUTS",
                      "source_m3_run_id": M3_RUN_ID, "routed_discharge_csv": str(route_csv),
                      "routed_discharge_manifest": str(route_json),
                      "terrain_provenance": terrain_meta,
                      "comparison_section_id": SECTION_ID,
                      "caveats": list(dict.fromkeys([*case_meta.get("caveats", []),
                                                       "mvp_nearfield_comparison",
                                                       "mvp_speed_magnitude_as_normal_velocity",
                                                       "not_fully_coupled_production_simulation"]))})
    generator.write_case(spec, case_meta, run_dir, terrain_dir)

    registry.init_db()
    conn = registry.connect()
    try:
        job_id = jobs.create_job(conn, "campaign", SITE_ID, payload={
            "model": "sph", "sph_campaign": True, "data_dir": str(data_dir),
            "terrain_dir": str(terrain_dir), "binaries_dir": str(binaries_dir),
            "sph_settings": {k: getattr(settings, k) for k in
                             ("dp_m", "t_start_s", "t_end_s", "time_out_s", "inlet_width_m",
                              "inlet_height_m", "inlet_layers", "boundary_layers", "surface_interval_s",
                              "vram_budget_mib", "vram_margin")},
        })
        jobs.update_payload(conn, job_id, run_ids=[M4_RUN_ID])
        with conn:
            conn.execute("INSERT OR IGNORE INTO scenarios (scenario_id, site_id, kind, params_json, created_at) VALUES (?, ?, 'named', ?, ?)",
                         (SCENARIO_ID, SITE_ID, json.dumps({"source_m3_run_id": M3_RUN_ID,
                          "domain_status": DOMAIN_STATUS, "routed_discharge": str(route_json)}), registry.utc_now()))
            retry_meta = {"case_dir": str(run_dir / "case"), "attempt": attempt, **case_meta}
            if registered:
                conn.execute("UPDATE runs SET status='queued', error=NULL, meta_json=?, finished_at=NULL WHERE run_id=?",
                             (json.dumps(retry_meta), M4_RUN_ID))
            else:
                conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json) VALUES (?, ?, 'sph', 'queued', ?, ?)",
                             (M4_RUN_ID, SCENARIO_ID, str(run_dir), json.dumps(retry_meta)))
        jobs.set_progress(conn, job_id, 0, 1, "runs")
        jobs.set_stage(conn, job_id, "queued", "simulating")
        jobs.log_event(conn, job_id, f"registered {M4_RUN_ID} from real M3 routed-discharge artifact")
        return job_id
    finally:
        conn.close()

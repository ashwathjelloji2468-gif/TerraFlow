"""M4 post-processing (`docs/handoff_contract.md` §4.4): turns a completed near-field run's raw
solver output into the same-schema summary a Delft3D run would produce --
`summary_nearfield/{max_depth,max_velocity,arrival_time}.tif`, `surfaces/t<seconds>.glb`,
`timeseries.csv`, `run_meta.json`. Never launches the solver itself (CLAUDE.md rule 14); this
only runs once a run's `raw/` directory already holds its particle data.

**Grid columns, not the near-field cell size.** MeasureTool interpolates SPH particles, not
raster cells, so the near-field canonical grid (`grid_nearfield.json`) is used only to pick where
to sample -- one column per cell centre, processed `settings.postprocess_row_chunk` rows at a
time so no full-grid points file or CSV is ever held in memory at once (CLAUDE.md rule 13).

**Near-field domain.** There is no `domain_mask_nearfield.tif` in the contract; a cell counts as
"inside the near-field domain" exactly when `dem_nearfield.tif` is valid there -- the same test
`generator.py` already uses to place probes and the inlet (`docs/decisions.md`, today's session).

**Depth**: `MeasureTool -elevation` per column, candidate z from the cell's own bed elevation up
to `bed + inlet_height_m` (the same depth ceiling `generator.py` uses for its VRAM estimate) --
`measuretool.py`'s docstring explains why a dry column reports exactly its bed elevation, giving
`depth = 0.0` for free. The candidate step (`elevation_dz_dp_fraction * dp_m`) has to stay under
the run's particle smoothing length or MeasureTool silently stops collapsing a column's candidates
into one elevation reading and reports each candidate as its own column instead (verified against
the real binary, `docs/decisions.md` today's session) -- scaling the step with `dp_m` keeps it
safely under that length for any case, since the smoothing length itself scales with `dp_m`. If
any cell's max depth comes within one such step of the ceiling, the run gets the
`sph_depth_search_capped` caveat (the true depth may be underestimated).

**Velocity**: depth-averaged, not a single fixed-height point (`docs/decisions.md` "SPH velocity:
depth-average, not a fixed-height point" -- comparing Delft3D's depth-averaged value against an
SPH point sample would read as model disagreement when it's really a definition mismatch). Only
cells with `max_depth > 0` are sampled (`settings.velocity_levels` explicit points per cell,
evenly spaced from bed to that cell's own max depth); at each timestep the horizontal speed
(`sqrt(vx^2+vy^2)`) is averaged over the levels at or below that timestep's elevation, then
maxed over time. Every near-field SPH case has particle spacing far coarser than the project's
0.1 m arrival threshold, so every run's caveats include `sph_arrival_below_resolution`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio

from backend.shared.grid import FLOAT_NODATA, CanonicalGrid, write_grid_raster

from . import gauges, measuretool, solver_log, surfaces
from .generator import probes_in_nearfield
from .settings import SphSettings, load_sph_settings
from backend.shared.probes import load_probes

CONTRACT_VERSION = "0.3.0"
ARRIVAL_THRESHOLD_M = 0.1  # contract §1.5 -- mirrors backend/m5_emulator/{synthetic,fallback}.py
EXTENT_THRESHOLD_M = 0.3
_WET_EPS_M = 1e-6


def _cell_centres_utm(grid: CanonicalGrid, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = grid.origin_x + (cols.astype(float) + 0.5) * grid.cell_size_m
    y = grid.origin_y - (rows.astype(float) + 0.5) * grid.cell_size_m
    return x, y


def _row_chunks(height: int, chunk_size: int):
    for start in range(0, height, chunk_size):
        yield start, min(start + chunk_size, height)


def compute_summary_rasters(
    dirdata: str | Path, grid_near: CanonicalGrid, dem_near: np.ndarray, frame: dict, dp_m: float,
    settings: SphSettings, work_dir: str | Path, binaries_dir: str | None = None,
) -> dict:
    """`{"max_depth": (H,W), "max_velocity": (H,W), "arrival_time": (H,W), "tau_s": (T,),
    "depth_capped": bool}` -- the three arrays on `grid_near`, nodata (`FLOAT_NODATA`) outside
    the near-field domain; `tau_s` is the solver's own PART time axis (seconds since the case's
    `t_start_s`), read once from the first non-empty chunk. `dp_m` is the case's own particle
    spacing (`case_meta.json`'s `dp_m`), used only to size the elevation search step (module
    docstring); it isn't otherwise read here."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    height, width = grid_near.shape
    elevation_dz_m = dp_m * settings.elevation_dz_dp_fraction

    max_depth = np.full((height, width), FLOAT_NODATA, dtype=np.float32)
    max_velocity = np.full((height, width), FLOAT_NODATA, dtype=np.float32)
    arrival_time = np.full((height, width), FLOAT_NODATA, dtype=np.float32)
    tau_s: np.ndarray | None = None
    depth_capped = False

    for r0, r1 in _row_chunks(height, settings.postprocess_row_chunk):
        block = dem_near[r0:r1]
        local_rc = np.argwhere(block != FLOAT_NODATA)
        if local_rc.size == 0:
            continue
        rows = local_rc[:, 0] + r0
        cols = local_rc[:, 1]
        bed_z = dem_near[rows, cols].astype(float)
        x_utm, y_utm = _cell_centres_utm(grid_near, rows, cols)
        x_local, y_local = x_utm - frame["origin_x"], y_utm - frame["origin_y"]

        ceiling = bed_z + settings.inlet_height_m
        columns = list(zip(x_local, y_local, bed_z, [elevation_dz_m] * len(bed_z), ceiling))
        points_path = measuretool.write_column_points(work_dir / f"depth_cols_{r0}.txt", columns)
        elev_csv = measuretool.run_elevation(dirdata, points_path, work_dir / f"depth_{r0}", binaries_dir)
        chunk_tau, elevation = measuretool.parse_elevation_csv(
            elev_csv, list(zip(x_local.tolist(), y_local.tolist())))
        if tau_s is None:
            tau_s = chunk_tau

        depth = np.clip(elevation - bed_z[None, :], 0.0, None)
        chunk_max_depth = depth.max(axis=0)
        if np.any(chunk_max_depth >= ceiling - bed_z - 1.5 * elevation_dz_m):
            depth_capped = True

        wet = depth > ARRIVAL_THRESHOLD_M
        any_wet = wet.any(axis=0)
        first_wet_idx = np.argmax(wet, axis=0)  # 0 where never wet; overwritten by any_wet below
        chunk_arrival = np.where(any_wet, chunk_tau[first_wet_idx], FLOAT_NODATA)

        chunk_max_velocity = np.zeros_like(chunk_max_depth)
        wet_idx = np.where(chunk_max_depth > _WET_EPS_M)[0]
        if wet_idx.size:
            n_levels = settings.velocity_levels
            level_z = np.linspace(
                bed_z[wet_idx], bed_z[wet_idx] + chunk_max_depth[wet_idx], n_levels, axis=1,
            )  # (n_wet, n_levels)
            vel_points = [
                (x_local[i], y_local[i], z) for i, zs in zip(wet_idx, level_z) for z in zs
            ]
            vel_points_path = measuretool.write_explicit_points(work_dir / f"vel_pts_{r0}.txt", vel_points)
            vel_csv = measuretool.run_velocity(dirdata, vel_points_path, work_dir / f"vel_{r0}", binaries_dir)
            vel_tau, vel_xyz = measuretool.parse_velocity_csv(vel_csv)
            if len(vel_tau) != len(chunk_tau):
                raise ValueError(
                    f"row chunk {r0}: elevation and velocity passes returned different PART "
                    f"counts ({len(chunk_tau)} vs {len(vel_tau)}) -- same dirdata should give the same axis"
                )
            vel_xyz = vel_xyz.reshape(len(vel_tau), wet_idx.size, n_levels, 3)
            elevation_wet = elevation[:, wet_idx]  # (T, n_wet)
            submerged = level_z[None, :, :] <= elevation_wet[:, :, None]  # (T, n_wet, n_levels)
            submerged[:, :, 0] = True  # the bed-level sample always counts once a cell is wet
            horizontal = np.hypot(vel_xyz[..., 0], vel_xyz[..., 1])
            level_mean = np.nanmean(np.where(submerged, horizontal, np.nan), axis=2)
            chunk_max_velocity[wet_idx] = level_mean.max(axis=0)

        max_depth[rows, cols] = chunk_max_depth
        max_velocity[rows, cols] = chunk_max_velocity
        arrival_time[rows, cols] = chunk_arrival

    return {
        "max_depth": max_depth, "max_velocity": max_velocity, "arrival_time": arrival_time,
        "tau_s": tau_s if tau_s is not None else np.zeros(0), "depth_capped": depth_capped,
    }




def _parse_solver_log(log_text: str | None) -> dict:
    """Best-effort facts from a GenCase/solver log, kept `None` (contract rule 3: never invent a
    fact) rather than guessed when the log wasn't captured or doesn't contain them."""
    facts = solver_log.parse_log(log_text)
    return {"particle_count": facts["total_particles"], "solver_version": facts["solver_version"], "facts": facts}


def build_run_meta(
    case_meta: dict, grid_near: CanonicalGrid, sim_duration_s: float, depth_capped: bool,
    log_text: str | None = None, started_at: str | None = None, execution: dict | None = None,
    max_excluded_particle_fraction: float | None = None,
) -> dict:
    """`run_meta.json` (contract §4.4), for the run `case_meta` (`generator.py`'s `case_meta.json`)
    describes. Facts this stage can't establish without a captured solver log --
    `solver_version`, `particle_count` (falls back to the pre-run VRAM estimate, flagged in
    `warnings`), `peak_vram_mb`, `wall_time_s`, `mass_balance_error_pct` -- stay `None`."""
    log_facts = _parse_solver_log(log_text)
    warnings: list[str] = []
    particle_count = log_facts["particle_count"]
    if particle_count is None:
        particle_count = case_meta.get("vram_total_particles")
        if particle_count is not None:
            warnings.append("particle_count is the pre-run VRAM estimate (no solver log captured), not GenCase's actual count")
    if log_facts["solver_version"] is None:
        warnings.append("solver_version NOT STATED -- no solver log captured for this run")
    execution = execution or {}
    peak_vram_mb = execution.get("peak_vram_mb")
    wall_time_s = execution.get("wall_time_s")
    if peak_vram_mb is None:
        warnings.append("peak_vram_mb not measured -- no nvidia-smi samples were recorded for this run")
    if wall_time_s is None:
        warnings.append("wall_time_s not recorded -- no launcher execution record for this run")
    warnings.append("mass_balance_error_pct not computed for SPH runs yet")

    caveats = list(case_meta.get("caveats", []))
    if case_meta["dp_m"] > ARRIVAL_THRESHOLD_M:
        caveats.append("sph_arrival_below_resolution")
    if depth_capped:
        caveats.append("sph_depth_search_capped")

    now = started_at or datetime.now(timezone.utc).isoformat()
    return {
        "contract_version": CONTRACT_VERSION,
        "run_id": f"{case_meta['scenario_id']}__sph", "scenario_id": case_meta["scenario_id"], "model": "sph",
        "status": "postprocessed",
        "solver_version": log_facts["solver_version"],
        "resolution_m": grid_near.cell_size_m, "dp_m": case_meta["dp_m"],
        "particle_count": particle_count, "peak_vram_mb": peak_vram_mb,
        "sim_duration_s": sim_duration_s, "wall_time_s": wall_time_s,
        "mass_balance_error_pct": None,
        "thresholds": {"extent_m": EXTENT_THRESHOLD_M, "arrival_m": ARRIVAL_THRESHOLD_M},
        "hydrographs": [],  # hydrograph is consumed in-process (contract §4.2); no CSV artifact
        "resampling": "none -- MeasureTool interpolated directly at each near-field cell centre",
        "warnings": warnings,
        "caveats": caveats,
        "has_placeholders": case_meta.get("has_placeholders", False),
        "placeholder_fields": case_meta.get("placeholder_fields", []),
        "started_at": execution.get("started_at", now), "finished_at": execution.get("finished_at", now),
        "solver_log_facts": log_facts["facts"],
        **({"particle_retention": solver_log.particle_retention(log_facts["facts"], max_excluded_particle_fraction)}
           if max_excluded_particle_fraction is not None else {}),
    }


def postprocess_run(
    run_dir: str | Path, terrain_dir: str | Path, dirdata: str | Path,
    settings: SphSettings | None = None, binaries_dir: str | None = None,
    execution: dict | None = None,
) -> dict:
    """Full contract §4.4 post-processing for a completed near-field run: `summary_nearfield/`,
    `surfaces/`, `timeseries.csv`, `run_meta.json`, all under `run_dir`. Returns the written
    `run_meta` dict. `dirdata` is the solver's raw PART output directory (`raw/data/` by
    convention -- see `main()`); `terrain_dir` is `data/<site_id>/terrain/`."""
    run_dir, terrain_dir = Path(run_dir), Path(terrain_dir)
    settings = settings or load_sph_settings()
    case_meta = json.loads((run_dir / "case" / "case_meta.json").read_text(encoding="utf-8"))

    grid_near = CanonicalGrid.from_json(terrain_dir / "grid_nearfield.json")
    frame = json.loads((terrain_dir / "nearfield_frame.json").read_text(encoding="utf-8"))
    with rasterio.open(terrain_dir / "dem_nearfield.tif") as ds:
        dem_near = ds.read(1)

    work_dir = run_dir / "raw" / "measuretool"
    rasters = compute_summary_rasters(dirdata, grid_near, dem_near, frame, case_meta["dp_m"], settings, work_dir, binaries_dir)

    summary_dir = run_dir / "summary_nearfield"
    summary_dir.mkdir(parents=True, exist_ok=True)
    write_grid_raster(summary_dir / "max_depth.tif", rasters["max_depth"], grid_near)
    write_grid_raster(summary_dir / "max_velocity.tif", rasters["max_velocity"], grid_near)
    write_grid_raster(summary_dir / "arrival_time.tif", rasters["arrival_time"], grid_near)

    probes = load_probes(terrain_dir)
    kept_probes, _ = probes_in_nearfield(probes, frame, grid_near, dem_near)
    expected_gauges = [
        run_dir / "raw" / f"GaugesSWL_swl_{p.probe.poi_id}.csv" for p in kept_probes
    ] + [
        run_dir / "raw" / f"GaugesVel_vel_{p.probe.poi_id}.csv" for p in kept_probes
    ]
    gauges_missing = [p.name for p in expected_gauges if not p.is_file()]
    if not gauges_missing:
        rows = gauges.build_timeseries(run_dir / "raw", kept_probes, case_meta["t_start_s"])
        gauges.write_timeseries_csv(run_dir / "timeseries.csv", rows)

    tau_s = rasters["tau_s"]
    iso_prefix = work_dir / "surface"
    surfaces.run_isosurface(dirdata, iso_prefix, binaries_dir)
    surfaces_dir = run_dir / "surfaces"
    surfaces_dir.mkdir(parents=True, exist_ok=True)
    surfaces.convert_surfaces(
        iso_prefix, tau_s.tolist(), settings.surface_interval_s, case_meta["t_start_s"], surfaces_dir,
    )

    log_path = run_dir / "raw" / "log.txt"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else None
    sim_duration_s = float(tau_s[-1] - tau_s[0]) if tau_s.size else 0.0
    run_meta = build_run_meta(case_meta, grid_near, sim_duration_s, rasters["depth_capped"], log_text,
                              execution=execution,
                              max_excluded_particle_fraction=settings.max_excluded_particle_fraction)
    if gauges_missing:
        run_meta["warnings"].append(
            "probe timeseries unavailable; solver did not emit configured gauge CSVs: "
            + ", ".join(gauges_missing)
        )
        run_meta["caveats"].append("sph_probe_timeseries_unavailable")
    if log_text and solver_log.EXCLUSION_WARNING in log_text:
        run_meta["warnings"].append(
            "DualSPHysics reported more than 100% of current fluid particles excluded in a PART output; "
            "depth and velocity artifacts require physical review"
        )
        run_meta["caveats"].append("sph_particle_exclusion_warning")
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")
    return run_meta


def main(argv: list[str] | None = None) -> None:
    """Post-process a run directory whose solver has already finished (never launches the
    solver -- CLAUDE.md rule 14)."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--terrain-dir", required=True)
    parser.add_argument("--dirdata", default=None, help="default: <run-dir>/raw/data")
    parser.add_argument("--binaries-dir", default=None, help="default: $DSPH_BIN_DIR")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    dirdata = Path(args.dirdata) if args.dirdata else run_dir / "raw" / "data"
    run_meta = postprocess_run(run_dir, args.terrain_dir, dirdata, binaries_dir=args.binaries_dir)
    print(f"wrote {run_dir / 'run_meta.json'} ({run_meta['status']})")


if __name__ == "__main__":
    main()

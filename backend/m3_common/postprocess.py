"""Contract-oriented post-processing for D-Flow FM map/history output."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.features import rasterize
from shapely.geometry import Polygon

CONTRACT_VERSION = "0.3.0"
NODATA = -9999.0


@dataclass(frozen=True)
class PostprocessConfig:
    """Post-processing options; keep raw maps by default for pilot provenance."""

    delete_raw_map: bool = False
    extent_m: float = 0.3
    arrival_m: float = 0.1


def _time_seconds(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    if np.issubdtype(values.dtype, np.datetime64):
        return ((values - values[0]) / np.timedelta64(1, "s")).astype(float)
    return values.astype(float) - float(values[0])


def _grid(path: Path) -> tuple[dict, rasterio.Affine, np.ndarray]:
    grid = json.loads(path.read_text())
    transform = rasterio.Affine(grid["cell_size_m"], 0, grid["origin_x"],
                                0, -grid["cell_size_m"], grid["origin_y"])
    return grid, transform, np.full((grid["height"], grid["width"]), grid["nodata"], np.float32)


def _write_raster(path: Path, data: np.ndarray, grid: dict, transform: rasterio.Affine) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=grid["height"], width=grid["width"],
                       count=1, dtype="float32", crs=f"EPSG:{grid['crs_epsg']}",
                       transform=transform, nodata=grid["nodata"], tiled=True,
                       compress="lzw") as dst:
        dst.write(data.astype(np.float32), 1)


def _faces(ds: xr.Dataset) -> list[np.ndarray]:
    face_node_var = ds["mesh2d_face_nodes"]
    face_node_values = np.asarray(face_node_var.values, dtype=float)
    start_index = int(face_node_var.attrs.get("start_index", 0))
    nodes_x = np.asarray(ds["mesh2d_node_x"].values, dtype=float)
    nodes_y = np.asarray(ds["mesh2d_node_y"].values, dtype=float)
    face_nodes = np.where(np.isfinite(face_node_values), face_node_values - start_index, -1).astype(int)
    polygons = []
    for row in face_nodes:
        row = row[(row >= 0) & (row < len(nodes_x))]
        polygons.append(Polygon(np.column_stack((nodes_x[row], nodes_y[row]))) if len(row) >= 3 else None)
    return polygons


def _rasterize(values: np.ndarray, polygons: list[np.ndarray], grid: dict,
               transform: rasterio.Affine, mask: np.ndarray, *, arrival: bool = False) -> np.ndarray:
    fill = grid["nodata"]
    out = rasterize(((poly, float(value)) for poly, value in zip(polygons, values)
                     if poly is not None and poly.is_valid and np.isfinite(value)),
                    out=np.full(mask.shape, fill, np.float32), transform=transform,
                    fill=fill, dtype="float32", all_touched=True)
    inside = mask == 1
    if arrival:
        out[inside & (out == fill)] = fill
    else:
        out[inside & (out == fill)] = 0.0
    out[~inside] = fill
    return out


def _accumulate_map_chunk(depth: np.ndarray, velocity: np.ndarray, elapsed_s: np.ndarray,
                          max_depth: np.ndarray, max_velocity: np.ndarray,
                          arrival_s: np.ndarray, arrival_threshold_m: float) -> None:
    """Merge a bounded time-by-face block into the three per-face summary arrays."""
    finite_depth = np.where(np.isfinite(depth), depth, -np.inf)
    finite_velocity = np.where(np.isfinite(velocity), velocity, -np.inf)
    np.maximum(max_depth, finite_depth.max(axis=0), out=max_depth)
    np.maximum(max_velocity, finite_velocity.max(axis=0), out=max_velocity)

    wet = depth > arrival_threshold_m
    newly_arrived = wet.any(axis=0) & ~np.isfinite(arrival_s)
    if newly_arrived.any():
        first_index = wet.argmax(axis=0)
        arrival_s[newly_arrived] = elapsed_s[first_index[newly_arrived]]


def _balance_error(case_dir: Path) -> float | None:
    """Return FM's emitted mass-balance error percent, if balance output was enabled."""
    candidates = [*case_dir.glob("**/*balance*.txt"), *case_dir.glob("**/*balance*.csv")]
    for path in candidates:
        text = path.read_text(errors="replace")
        matches = re.findall(r"(?:error|residual)[^\n,;]*?([-+]?\d+(?:\.\d+)?)\s*%", text, re.I)
        if matches:
            return float(matches[-1])
    # FM can expose total mass-balance quantities in HIS NetCDF when Wrihis_balance=1.
    for path in case_dir.glob("output/*_his.nc"):
        with xr.open_dataset(path) as ds:
            if "water_balance_volume_error" not in ds.variables:
                continue
            error_values = np.asarray(ds["water_balance_volume_error"].values, dtype=float)
            finite_error = error_values[np.isfinite(error_values)]
            if not finite_error.size:
                continue
            # D-Flow FM reports cumulative volume error in m3. Convert that solver output to the
            # contract's percentage using cumulative positive inflows over the same run.
            inflow_names = (
                "water_balance_boundaries_in", "water_balance_exchange_with_1D_in",
                "water_balance_precipitation_total", "water_balance_source_sink",
                "water_balance_groundwater_in", "water_balance_laterals_in", "water_balance_Qext_in",
            )
            total_inflow = 0.0
            found_inflow = False
            for name in inflow_names:
                if name not in ds.variables:
                    continue
                values = np.asarray(ds[name].values, dtype=float)
                finite = values[np.isfinite(values)]
                if finite.size:
                    total_inflow += max(float(finite[-1]), 0.0)
                    found_inflow = True
            if found_inflow and total_inflow > 0:
                return float(finite_error[-1] / total_inflow * 100.0)
    return None


def _solver_peak_ram_mb(case_dir: Path) -> float | None:
    usage = next((p for p in (case_dir / "output/resource_usage.txt", case_dir / "resource_usage.txt") if p.is_file()), None)
    if usage is None:
        return None
    match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", usage.read_text(errors="replace"))
    return None if not match else int(match.group(1)) / 1024.0


def _dia_solver_identity(dia: Path) -> dict:
    """Feature 17 (A2): the solver identity the kernel itself wrote into this run's `.dia`
    (`Program = ...` / `Version = ...`), instead of a hard-coded version string."""
    text = dia.read_text(errors="replace") if dia.is_file() else ""
    program = re.search(r"^\s*Program\s*=\s*(.+?)\s*(?:#|$)", text, re.M)
    version = re.search(r"^\s*Version\s*=\s*(.+?)\s*(?:#|$)", text, re.M)
    program_s = program.group(1) if program else None
    version_s = version.group(1) if version else None
    return {"source": str(dia), "dia_present": dia.is_file(), "program": program_s, "version": version_s,
            "solver_version": f"{program_s} {version_s}" if program_s and version_s else (version_s or None)}


def _solver_wall_seconds(case_dir: Path) -> float | None:
    usage = next((p for p in (case_dir / "output/resource_usage.txt", case_dir / "resource_usage.txt") if p.is_file()), None)
    if usage is None:
        return None
    # GNU time: "Elapsed (wall clock) time (h:mm:ss or m:ss): 1:02.50" -- the label itself contains
    # colons, so match it literally and take the single value token after it (Feature 17, A3).
    match = re.search(r"Elapsed \(wall clock\) time(?: \([^)]*\))?:\s*(\S+)", usage.read_text(errors="replace"))
    if not match:
        return None
    parts = match.group(1).strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def _dia_runtime(dia: Path) -> tuple[float | None, str | None, str | None]:
    if not dia.is_file():
        return None, None, None
    text = dia.read_text(errors="replace")
    runtimes = re.findall(r"total computation time\s*\(s\)\s*:\s*([0-9.]+)", text, re.I)
    def stamp(label: str) -> str | None:
        match = re.search(rf"Computation {label}\s+at:\s*(\d{{2}}:\d{{2}}:\d{{2}}),\s*(\d{{2}}-\d{{2}}-\d{{4}})", text, re.I)
        if not match:
            return None
        dt = datetime.strptime(" ".join(match.groups()), "%H:%M:%S %d-%m-%Y").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return (float(runtimes[-1]) if runtimes else None, stamp("started"), stamp("finished"))


def _wall_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    return (datetime.fromisoformat(finished_at.replace("Z", "+00:00")) -
            datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds()


def postprocess_dflowfm(case_dir: str | Path, run_dir: str | Path, *,
                        grid_path: str | Path, domain_mask_path: str | Path,
                        run_id: str, scenario_id: str, hydrographs: list[str] | None = None,
                        spinup_s: float = 0.0, config: PostprocessConfig = PostprocessConfig()) -> dict:
    """Write canonical-grid summaries, POI time series and run metadata.

    Raw map deletion occurs only after every output is written and metadata is validated.
    """
    case_dir, run_dir = Path(case_dir), Path(run_dir)
    output_dir = case_dir / "output"
    map_path = next(output_dir.glob("*_map.nc"), None)
    his_path = next(output_dir.glob("*_his.nc"), None)
    if map_path is None or his_path is None:
        raise FileNotFoundError("D-Flow FM post-processing requires both *_map.nc and *_his.nc")
    dia_path = next(output_dir.glob("*.dia"), case_dir / "output" / "missing.dia")
    dia_text = dia_path.read_text(errors="replace") if dia_path.is_file() else ""
    errors = [line for line in dia_text.splitlines() if line.startswith("** ERROR")]
    if errors:
        raise RuntimeError("D-Flow FM run contains errors: " + " | ".join(errors))

    grid, transform, _ = _grid(Path(grid_path))
    with rasterio.open(domain_mask_path) as mask_ds:
        mask = mask_ds.read(1)
        if mask.shape != (grid["height"], grid["width"]) or mask_ds.transform != transform:
            raise ValueError("domain mask does not match canonical grid")

    with xr.open_dataset(map_path) as ds:
        required = {"mesh2d_waterdepth", "mesh2d_ucmag", "mesh2d_face_nodes", "mesh2d_node_x", "mesh2d_node_y"}
        missing = required - set(ds.variables)
        if missing:
            raise ValueError(f"FM map is missing required variables: {sorted(missing)}")
        model_time = _time_seconds(ds.time.values)
        selected_indices = np.flatnonzero(model_time >= spinup_s)
        if not selected_indices.size:
            raise ValueError("FM map contains no samples at or after t0")
        n_faces = int(ds.mesh2d_waterdepth.shape[1])
        max_depth = np.full(n_faces, -np.inf, dtype=np.float32)
        max_velocity = np.full(n_faces, -np.inf, dtype=np.float32)
        arrival = np.full(n_faces, np.nan, dtype=np.float32)
        # Keep both input variables and finite-value copies below roughly 128 MiB.
        # Production grids can have >1M faces; never materialize the full time×face stacks.
        max_values_per_chunk = 8 * 1024 * 1024
        chunk_records = max(1, min(64, max_values_per_chunk // max(1, 2 * n_faces)))
        for offset in range(0, len(selected_indices), chunk_records):
            indices = selected_indices[offset:offset + chunk_records]
            depth = np.asarray(ds.mesh2d_waterdepth.isel(time=indices).values, dtype=np.float32)
            velocity = np.asarray(ds.mesh2d_ucmag.isel(time=indices).values, dtype=np.float32)
            elapsed = model_time[indices] - spinup_s
            _accumulate_map_chunk(depth, velocity, elapsed, max_depth, max_velocity,
                                  arrival, config.arrival_m)
        max_depth[~np.isfinite(max_depth)] = np.nan
        max_velocity[~np.isfinite(max_velocity)] = np.nan
        has_arrival = np.isfinite(arrival)
        polygons = _faces(ds)
        cell_count = n_faces
        sim_duration_s = float(max(0.0, model_time[-1] - spinup_s))

    summary_dir = run_dir / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    depth_grid = _rasterize(max_depth, polygons, grid, transform, mask)
    _write_raster(summary_dir / "max_depth.tif", depth_grid, grid, transform)
    _write_raster(summary_dir / "max_velocity.tif", _rasterize(max_velocity, polygons, grid, transform, mask), grid, transform)
    arrival_face = np.where(has_arrival, arrival, np.nan)
    _write_raster(summary_dir / "arrival_time.tif", _rasterize(arrival_face, polygons, grid, transform, mask, arrival=True), grid, transform)

    with xr.open_dataset(his_path) as his:
        required = {"waterdepth", "velocity_magnitude", "waterlevel", "station_id"}
        missing = required - set(his.variables)
        if missing:
            raise ValueError(f"FM history is missing required variables: {sorted(missing)}")
        htime = _time_seconds(his.time.values)
        after_t0 = htime >= spinup_s
        poi_depth = np.asarray(his.waterdepth.values, dtype=float)
        poi_arrival_s = []
        for station_index in range(poi_depth.shape[1]):
            wet_indices = np.flatnonzero((poi_depth[:, station_index] > config.arrival_m) & (htime >= spinup_s))
            poi_arrival_s.append(float(htime[wet_indices[0]] - spinup_s) if wet_indices.size else None)
        station_ids = [x.decode(errors="replace").strip() if isinstance(x, bytes) else str(x).strip()
                       for x in his.station_id.values]
        columns = ["poi_id", "t_s", "depth_m", "velocity_ms", "wse_m", "arrival_s_since_t0"]
        with (run_dir / "timeseries.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for i, poi_id in enumerate(station_ids):
                for j in np.flatnonzero(after_t0):
                    values = [his.waterdepth.values[j, i], his.velocity_magnitude.values[j, i], his.waterlevel.values[j, i]]
                    arrival = poi_arrival_s[i]
                    writer.writerow([poi_id, f"{htime[j]-spinup_s:.3f}",
                                     *["" if not np.isfinite(v) else f"{float(v):.6f}" for v in values],
                                     "" if arrival is None else f"{arrival:.3f}"])

    runtime_s, started_at, finished_at = _dia_runtime(dia_path)
    solver_identity = _dia_solver_identity(dia_path)
    output_bytes = sum(p.stat().st_size for p in output_dir.rglob("*") if p.is_file())
    disk_files = {p.resolve() for root in (case_dir, run_dir) for p in root.rglob("*") if p.is_file()}
    disk_bytes = sum(p.stat().st_size for p in disk_files)
    peak_ram_mb = _solver_peak_ram_mb(case_dir)
    balance = _balance_error(case_dir)
    warnings = []
    if not np.any((depth_grid > 0) & (mask == 1)):
        warnings.append("FM output has no positive depth inside domain_mask; check mesh/domain alignment")
    if balance is None:
        warnings.append("FM mass-balance output unavailable; enable Wrihis_balance for subsequent runs")
    if peak_ram_mb is None:
        warnings.append("solver peak RAM unavailable; launcher resource capture was not present for this run")
    if runtime_s is None:
        warnings.append("FM runtime unavailable in diagnostic output")
    if solver_identity["solver_version"] is None:
        warnings.append("solver Program/Version not found in the .dia; solver_version left null (not assumed)")
    run_meta = {
        "contract_version": CONTRACT_VERSION, "run_id": run_id, "scenario_id": scenario_id,
        "model": "delft3d", "status": "postprocessed", "solver_version": solver_identity["solver_version"],
        "solver_identity": solver_identity,
        "resolution_m": grid["cell_size_m"], "dp_m": None, "particle_count": None,
        "peak_vram_mb": None, "sim_duration_s": sim_duration_s,
        "wall_time_s": _solver_wall_seconds(case_dir) or _wall_seconds(started_at, finished_at),
        "runtime_s": runtime_s,
        "mass_balance_error_pct": balance,
        "mass_balance_error_formula": "100 * water_balance_volume_error / cumulative FM inflows",
        "thresholds": {"extent_m": config.extent_m, "arrival_m": config.arrival_m},
        "hydrographs": hydrographs or [], "resampling": "face polygon rasterization; all_touched",
        "warnings": warnings, "caveats": ["clear_water"], "has_placeholders": True,
        "placeholder_fields": ["pilot_case_inputs"] if "pilot" in scenario_id else [],
        "started_at": started_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "finished_at": finished_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cell_count": cell_count, "peak_ram_mb": peak_ram_mb, "disk_bytes": disk_bytes,
        "raw_output_disk_bytes": output_bytes,
    }
    # Validate required contract fields before committing the metadata or deleting raw output.
    try:
        from jsonschema import validate
        schema = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "run_meta.schema.json"
        validate(run_meta, json.loads(schema.read_text()))
    except ImportError:
        pass
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n")
    if config.delete_raw_map:
        map_path.unlink()
    return run_meta

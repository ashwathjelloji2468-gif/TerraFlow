"""Real-kernel smoke harness for one D-Flow FM case (Feature 5, phase 1).

Runs the *configured* kernel through the production launcher (`launcher.launch_case`) and reports
three separate verdicts, because a solver that finished is not automatically a physically useful
simulation:

- `solver_success`      -- M3 rule 1: no `** ERROR` in the `.dia`, `_map.nc` and `_his.nc` exist
                           (the exit code is deliberately not consulted).
- `postprocess_success` -- `m3_common.postprocess.postprocess_dflowfm` wrote the canonical summaries.
- `wet_at_pois`         -- each POI's maximum history depth after spin-up exceeds the post-processing
                           arrival threshold (`PostprocessConfig.arrival_m`).

It also proves the run came from the configured kernel: `/usr/bin/time -v` records the command
line in `output/resource_usage.txt`, and the `.dia` must have been written after launch and name
`Program = D-Flow FM`. Nothing here simulates, fakes or substitutes solver output.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import numpy as np

from . import launcher

_COMMAND_RE = re.compile(r'Command being timed:\s*"(.*)"')
_EXIT_RE = re.compile(r"Exit status:\s*(-?\d+)")
# GNU time prints "Elapsed (wall clock) time (h:mm:ss or m:ss): 1:02.50".
_WALL_RE = re.compile(r"Elapsed \(wall clock\) time(?: \([^)]*\))?:\s*([^\n]+)")
_PROGRAM_RE = re.compile(r"^\s*Program\s*=\s*(.+?)\s*(?:#|$)", re.M)
_VERSION_RE = re.compile(r"^\s*Version\s*=\s*(.+?)\s*(?:#|$)", re.M)
_XY_RE = re.compile(r"^\s*([xy])Coordinates\s*=\s*([-+0-9.eE]+)", re.I | re.M)


class KernelUnavailable(RuntimeError):
    """The configured kernel is missing or not executable -- nothing is run."""


def resolve_kernel(kernel: str | Path | None = None) -> Path:
    """`kernel`, else `$SIH26_DFLOWFM_KERNEL`; must be an executable file. No default fallback."""
    value = kernel if kernel is not None else os.environ.get("SIH26_DFLOWFM_KERNEL")
    if not value:
        raise KernelUnavailable("SIH26_DFLOWFM_KERNEL is not set")
    path = Path(value).expanduser()
    if not path.is_file():
        raise KernelUnavailable(f"kernel not found: {path}")
    if not os.access(path, os.X_OK):
        raise KernelUnavailable(f"kernel is not executable: {path}")
    return path.resolve()


def _wall_seconds(text: str) -> float | None:
    match = _WALL_RE.search(text)
    if not match:
        return None
    parts = match.group(1).strip().split(":")
    try:
        secs = 0.0
        for part in parts:
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        return None


def parse_resource_usage(path: Path) -> dict:
    """The `/usr/bin/time -v` record: command, exit status, wall seconds, peak RSS (MiB)."""
    if not path.is_file():
        return {"present": False}
    text = path.read_text(errors="replace")
    command = _COMMAND_RE.search(text)
    exit_status = _EXIT_RE.search(text)
    rss = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", text)
    return {"present": True, "command": command.group(1) if command else None,
            "exit_status": int(exit_status.group(1)) if exit_status else None,
            "wall_time_s": _wall_seconds(text),
            "peak_ram_mb": int(rss.group(1)) / 1024.0 if rss else None}


def parse_dia_identity(dia: Path) -> dict:
    """`Program` / `Version` that D-Flow FM echoes into its `.dia`."""
    if not dia.is_file():
        return {"present": False}
    text = dia.read_text(errors="replace")
    program, version = _PROGRAM_RE.search(text), _VERSION_RE.search(text)
    return {"present": True, "program": program.group(1) if program else None,
            "version": version.group(1) if version else None,
            "mtime": dia.stat().st_mtime}


def kernel_provenance(case_dir: Path, model_stem: str, kernel: Path, launched_at: float) -> dict:
    """Evidence that this run's output came from `kernel` and from this launch."""
    usage = parse_resource_usage(case_dir / "output" / "resource_usage.txt")
    dia = parse_dia_identity(case_dir / "output" / f"{model_stem}.dia")
    command = usage.get("command") or ""
    checks = {
        "resource_usage_present": usage["present"],
        "command_names_configured_kernel": str(kernel) in command,
        "dia_present": dia["present"],
        "dia_program_is_dflowfm": (dia.get("program") or "").startswith("D-Flow FM"),
        # 1 s slack for filesystems with coarse mtimes.
        "dia_written_after_launch": dia["present"] and dia["mtime"] >= launched_at - 1.0,
    }
    return {"kernel": str(kernel), "resource_usage": usage, "dia": dia, "checks": checks,
            "verified": all(checks.values())}


def _source_xy(case_dir: Path) -> tuple[float, float] | None:
    ext = case_dir / "inputs" / "forcing.ext"
    if not ext.is_file():
        return None
    found = {m.group(1).lower(): float(m.group(2)) for m in _XY_RE.finditer(ext.read_text(errors="replace"))}
    return (found["x"], found["y"]) if {"x", "y"} <= set(found) else None


def _outlet_xy(case_dir: Path) -> tuple[float, float] | None:
    pli = case_dir / "inputs" / "downstream_outlet.pli"
    if not pli.is_file():
        return None
    rows = []
    for line in pli.read_text(errors="replace").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 2:
            try:
                rows.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return tuple(float(v) for v in np.mean(rows, axis=0)) if rows else None


def downstream_wet_faces(map_path: Path, source_xy: tuple[float, float],
                         outlet_xy: tuple[float, float]) -> dict:
    """Faces whose centroid is nearer the downstream outlet than the breach source and whose
    maximum water depth over the run is strictly positive. Reads the map in time chunks."""
    import xarray as xr

    with xr.open_dataset(map_path) as ds:
        if "mesh2d_face_x" in ds.variables and "mesh2d_face_y" in ds.variables:
            fx = np.asarray(ds["mesh2d_face_x"].values, dtype=float)
            fy = np.asarray(ds["mesh2d_face_y"].values, dtype=float)
        else:  # centroid of each face's nodes
            nodes = np.asarray(ds["mesh2d_face_nodes"].values, dtype=float)
            start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
            valid = np.isfinite(nodes)
            idx = np.where(valid, nodes - start, 0).astype(int)
            nx, ny = (np.asarray(ds[v].values, dtype=float) for v in ("mesh2d_node_x", "mesh2d_node_y"))
            count = valid.sum(axis=1)
            fx = np.where(valid, nx[idx], 0.0).sum(axis=1) / count
            fy = np.where(valid, ny[idx], 0.0).sum(axis=1) / count
        depth = ds["mesh2d_waterdepth"]
        max_depth = np.full(fx.shape, -np.inf)
        for start in range(0, depth.sizes["time"], 32):
            block = np.asarray(depth.isel(time=slice(start, start + 32)).values, dtype=float)
            max_depth = np.fmax(max_depth, np.nanmax(block, axis=0))
    d_src = np.hypot(fx - source_xy[0], fy - source_xy[1])
    d_out = np.hypot(fx - outlet_xy[0], fy - outlet_xy[1])
    downstream = d_out < d_src
    wet = downstream & (max_depth > 0.0)
    return {"rule": "face centroid nearer the outlet than the breach source, max depth > 0 m",
            "downstream_faces": int(downstream.sum()), "wet_downstream_faces": int(wet.sum()),
            "max_downstream_depth_m": float(max_depth[downstream].max()) if downstream.any() else None}


def outlet_wet(map_path: Path, outlet_pli: Path) -> dict:
    """Faces touching the configured outlet polyline and whether any had max depth > 0 m."""
    import xarray as xr
    from shapely.geometry import LineString, Polygon

    rows = []
    for line in outlet_pli.read_text(errors="replace").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((float(parts[0]), float(parts[1])))
    line = LineString(rows).buffer(1.0)
    with xr.open_dataset(map_path) as ds:
        raw = np.asarray(ds["mesh2d_face_nodes"].values, dtype=float)
        start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
        nx, ny = (np.asarray(ds[v].values, dtype=float) for v in ("mesh2d_node_x", "mesh2d_node_y"))
        faces = [i for i, row in enumerate(raw)
                 if Polygon([(nx[int(v) - start], ny[int(v) - start]) for v in row if np.isfinite(v)]).intersects(line)]
        depth = ds["mesh2d_waterdepth"]
        peak = np.full(len(faces), -np.inf)
        for t0 in range(0, depth.sizes["time"], 32):
            block = np.asarray(depth.isel(time=slice(t0, t0 + 32)).values, dtype=float)[:, faces]
            peak = np.fmax(peak, np.nanmax(block, axis=0))
    return {"rule": "faces intersecting the outlet polyline (1 m buffer), max depth > 0 m",
            "outlet_faces": [int(i) for i in faces], "max_depth_m": [float(v) for v in peak],
            "wet": bool(len(faces)) and bool(np.any(peak > 0.0))}


def wet_at_pois(his_path: Path, spinup_s: float, threshold_m: float) -> dict:
    """Per-POI maximum depth after spin-up and whether it exceeds `threshold_m`."""
    import xarray as xr

    with xr.open_dataset(his_path) as his:
        t = np.asarray(his.time.values)
        t_s = ((t - t[0]) / np.timedelta64(1, "s")).astype(float) if np.issubdtype(t.dtype, np.datetime64) \
            else t.astype(float) - float(t[0])
        depth = np.asarray(his.waterdepth.values, dtype=float)
        ids = [x.decode(errors="replace").strip() if isinstance(x, bytes) else str(x).strip()
               for x in his.station_id.values]
    after = t_s >= spinup_s
    per_poi = {}
    for i, poi in enumerate(ids):
        col = depth[after, i] if after.any() else np.array([])
        peak = float(np.nanmax(col)) if col.size and np.isfinite(col).any() else None
        per_poi[poi] = {"max_depth_m": peak, "wet": peak is not None and peak > threshold_m}
    return {"threshold_m": threshold_m, "per_poi": per_poi,
            "all_wet": bool(per_poi) and all(v["wet"] for v in per_poi.values())}


def run_smoke_case(case_dir: str | Path, run_dir: str | Path, *, terrain_dir: str | Path,
                   kernel: str | Path | None = None, model: str = "model.mdu",
                   spinup_s: float = 0.0, run_id: str = "smoke__dflowfm",
                   scenario_id: str = "smoke", timeout_s: float = 1800.0) -> dict:
    """Run one existing case on the real kernel and write `<run_dir>/smoke_report.json`."""
    from backend.m3_common.postprocess import PostprocessConfig, postprocess_dflowfm

    kernel = resolve_kernel(kernel)
    case_dir, run_dir, terrain_dir = Path(case_dir).resolve(), Path(run_dir).resolve(), Path(terrain_dir)
    stem = Path(model).stem
    report: dict = {"case_dir": str(case_dir), "kernel": str(kernel), "solver": "D-Flow FM (real kernel)",
                    "solver_success": False, "postprocess_success": False, "wet_at_pois": None,
                    "notes": ["solver_success is M3 rule 1 only; it does not mean the flood result is "
                              "physically correct"]}
    if source_xy := _source_xy(case_dir):
        from .hydraulic_path import source_outlet_bottleneck
        report["mesh_hydraulic_path"] = source_outlet_bottleneck(
            case_dir / "inputs" / "domain_net.nc", source_xy, case_dir / "inputs" / "downstream_outlet.pli")
    launched_at = time.time()
    proc = launcher.launch_case(case_dir, run_dir, model=model, kernel=kernel)
    try:
        proc.wait(timeout=timeout_s)
        report["timed_out"] = False
    except subprocess.TimeoutExpired:
        report["timed_out"] = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            proc.kill()
        proc.wait()
    report["harness_wall_time_s"] = time.time() - launched_at

    solver = launcher.check_success(case_dir, stem)
    report["solver_check"] = solver
    report["solver_success"] = bool(solver["success"]) and not report["timed_out"]
    report["kernel_provenance"] = kernel_provenance(case_dir, stem, kernel, launched_at)
    report["wall_time_s"] = report["kernel_provenance"]["resource_usage"].get("wall_time_s")

    output = case_dir / "output"
    if report["solver_success"]:
        source, outlet = _source_xy(case_dir), _outlet_xy(case_dir)
        if source and outlet:
            report["downstream"] = downstream_wet_faces(output / f"{stem}_map.nc", source, outlet)
        report["outlet"] = outlet_wet(output / f"{stem}_map.nc", case_dir / "inputs" / "downstream_outlet.pli")
        try:
            meta = postprocess_dflowfm(case_dir, run_dir, grid_path=terrain_dir / "grid.json",
                                       domain_mask_path=terrain_dir / "domain_mask.tif", run_id=run_id,
                                       scenario_id=scenario_id, spinup_s=spinup_s)
            report["postprocess_success"] = True
            report["postprocess_warnings"] = meta.get("warnings", [])
        except Exception as e:  # reported, never hidden: post-processing is a separate verdict
            report["postprocess_error"] = f"{type(e).__name__}: {e}"
        report["wet_at_pois"] = wet_at_pois(output / f"{stem}_his.nc", spinup_s, PostprocessConfig().arrival_m)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "smoke_report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report

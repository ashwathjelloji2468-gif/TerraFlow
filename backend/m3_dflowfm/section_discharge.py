"""Discharge through a cross-section of a real D-Flow FM run (Feature 6: Feature 5 → M4 inflow).

Generalises the Teesta-MVP-only `m4_sph.teesta_mvp.extract_mvp_section`: any run directory, its own
`*_map.nc`, a caller-supplied section, any site/scenario/run id. Nothing Teesta-specific.

Q(t) = sum over faces cut by the section of  depth × speed × (cut length).

CAVEAT (kept in every artifact): the map stores speed *magnitude* (`mesh2d_ucmag`), not a velocity
vector, so the speed is treated as section-normal. Q is therefore an upper bound wherever flow
crosses the section obliquely.

Times are written as seconds since t0 (CLAUDE.md rule 6): the run's spin-up (`case_meta.json`
`spinup_s`) is subtracted and earlier records dropped.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .routed_discharge import write_routed_discharge

SPEED_CAVEAT = "speed magnitude (mesh2d_ucmag) treated as section-normal velocity: no velocity vector in the map"


def _case_dir(run_dir: Path) -> Path:
    meta_path = run_dir / "run_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text())
        if meta.get("case_dir"):
            candidate = Path(meta["case_dir"])
            return candidate if candidate.is_absolute() else run_dir / candidate
    for candidate in (run_dir / "dflowfm", run_dir / "case"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no D-Flow FM case directory under {run_dir}")


def find_map(case_dir: Path) -> Path:
    maps = sorted((case_dir / "output").glob("*_map.nc"))
    if len(maps) != 1:
        raise FileNotFoundError(f"expected exactly one *_map.nc in {case_dir / 'output'}, found {len(maps)}")
    return maps[0]


def normal_section(centreline, x: float, y: float, width_m: float):
    """A `width_m` LineString through (x, y) normal to `centreline` (site UTM)."""
    from shapely.geometry import LineString, Point

    s = centreline.project(Point(x, y))
    p0 = centreline.interpolate(max(0.0, s - 50.0))
    p1 = centreline.interpolate(min(centreline.length, s + 50.0))
    tx, ty = p1.x - p0.x, p1.y - p0.y
    norm = math.hypot(tx, ty)
    if norm == 0:
        raise ValueError("cannot derive a section normal from a degenerate centreline")
    tx, ty = tx / norm, ty / norm
    centre = centreline.interpolate(s)
    half = width_m / 2
    return LineString([(centre.x - ty * half, centre.y + tx * half), (centre.x + ty * half, centre.y - tx * half)])


def section_discharge(run_dir: str | Path, section, *, crs_epsg: int) -> dict:
    """Q(t) through `section` from the run's map; times in seconds since t0."""
    import xarray as xr
    from shapely.geometry import Polygon

    run_dir = Path(run_dir)
    case_dir = _case_dir(run_dir)
    map_path = find_map(case_dir)
    case_meta_path = case_dir / "case_meta.json"
    spinup_s = float(json.loads(case_meta_path.read_text()).get("spinup_s", 0.0)) if case_meta_path.is_file() else 0.0
    with xr.open_dataset(map_path) as ds:
        start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
        nodes = np.column_stack((ds.mesh2d_node_x.values, ds.mesh2d_node_y.values))
        ids, lengths = [], []
        for i, row in enumerate(ds.mesh2d_face_nodes.values):
            poly = Polygon(nodes[row[np.isfinite(row)].astype(int) - start])
            if poly.intersects(section):
                cut = poly.intersection(section).length
                if cut > 0:
                    ids.append(i)
                    lengths.append(cut)
        if not ids:
            raise ValueError("section does not cross the D-Flow FM mesh")
        t = np.asarray(ds.time.values)
        t_s = ((t - t[0]) / np.timedelta64(1, "s")).astype(float) if np.issubdtype(t.dtype, np.datetime64) \
            else t.astype(float) - float(t[0])
        depth = np.asarray(ds.mesh2d_waterdepth[:, ids].values, dtype=float)
        speed = np.asarray(ds.mesh2d_ucmag[:, ids].values, dtype=float)
    q = np.nansum(np.maximum(depth, 0.0) * np.maximum(speed, 0.0) * np.asarray(lengths)[None, :], axis=1)
    keep = t_s >= spinup_s
    if keep.sum() < 2:
        raise ValueError("D-Flow FM map has fewer than two records after spin-up")
    return {"t_s": t_s[keep] - spinup_s, "q_m3s": q[keep], "map": str(map_path), "spinup_s": spinup_s,
            "faces_crossed": len(ids), "max_section_depth_m": float(np.nanmax(depth[keep])),
            "section": {"type": "LineString", "coordinates": [list(c) for c in section.coords],
                        "crs": f"EPSG:{crs_epsg}", "length_m": float(section.length)}}


def _case_epsg(run_dir: Path) -> int | None:
    try:
        meta = json.loads((_case_dir(Path(run_dir)) / "case_meta.json").read_text())
    except (OSError, ValueError, FileNotFoundError):
        return None
    return int(meta["crs_epsg"]) if meta.get("crs_epsg") is not None else None


def write_section_discharge(run_dir: str | Path, output_dir: str | Path | None, section, *, site_id: str,
                            scenario_id: str, source_run_id: str, crs_epsg: int, section_id: str,
                            section_meta: dict | None = None) -> tuple[Path, Path]:
    """Extract and write the canonical routed-discharge artifact (`routed_discharge.json`).

    `output_dir=None` writes to the M3 run's own `routed_discharge/` (contract §4.4, decision E2).
    Feature 17: the section CRS must equal the case's `crs_epsg`; the method stays the controlled
    depth x speed-magnitude integral (decision S2 deferred) and its upper-bound caveat is recorded in
    `provenance.caveats`. The result is a CONTROLLED artifact (`artifact_schema_checked`), never a
    production-validated one."""
    from .routed_discharge import ARTIFACT_DIRNAME, check_run_id, check_scenario_id
    from .routing_section import CONTROLLED_METHOD_CAVEATS, CONTROLLED_METHOD_ID

    check_scenario_id(scenario_id)
    check_run_id(source_run_id)
    case_epsg = _case_epsg(Path(run_dir))
    if case_epsg is not None and case_epsg != int(crs_epsg):
        raise ValueError(f"section CRS EPSG:{crs_epsg} does not match the M3 case CRS EPSG:{case_epsg}")
    target = Path(output_dir) if output_dir is not None else Path(run_dir) / ARTIFACT_DIRNAME
    result = section_discharge(run_dir, section, crs_epsg=crs_epsg)
    return write_routed_discharge(
        target, site_id=site_id, scenario_id=scenario_id, source_run_id=source_run_id,
        t_s=result["t_s"], q_m3s=result["q_m3s"],
        routing_method="depth × speed-magnitude line integration across a D-Flow FM section",
        section={**result["section"], "id": section_id},
        provenance={"status": "REAL_M3_DERIVED", "source_map": result["map"], "source_run_id": source_run_id,
                    "extraction_mode": "controlled", "extraction_method_id": CONTROLLED_METHOD_ID,
                    "depth_variable": "mesh2d_waterdepth", "speed_variable": "mesh2d_ucmag",
                    "source_variables": ["mesh2d_waterdepth", "mesh2d_ucmag"],
                    "integration_convention": "sum over map faces cut by the section of max(depth,0) x "
                                              "max(speed,0) x cut length; face-centred values",
                    "velocity_vector_available": False, "normal_velocity_assumption": SPEED_CAVEAT,
                    "spinup_removed_s": result["spinup_s"], "faces_crossed": result["faces_crossed"],
                    "max_section_depth_m": result["max_section_depth_m"],
                    "time_reference": "seconds since t0 (map time minus case_meta spinup_s; CLAUDE.md rule 6)",
                    "case_crs_epsg": case_epsg, "section_meta": section_meta or {}},
        caveats=list(CONTROLLED_METHOD_CAVEATS),
    )

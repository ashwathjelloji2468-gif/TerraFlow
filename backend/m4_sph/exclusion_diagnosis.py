"""Evidence for the Teesta SPH particle-exclusion defect (Feature 6). Read-only; no solver run.

Recorded evidence so far (docs/progress.md 2026-09-28 "SPH outlet-zone diagnosis"): attempts a02/a03
excluded ~540,000 particles, 100% with `Motive=1` (position), starting at t=85 s at the bottom
corner of the inlet's own zone box, not near the outlet. Two mechanisms fit that evidence and are
not yet distinguished:

1. the inlet plane is anchored at the bed elevation of its *centre* point only; across a sloped
   cross-section part of the plane lies below the terrain, so injected particles start under the
   STL terrain sheet; and/or
2. `inputtreatment=2` removes fluid that re-enters the inlet zone.

`inlet_burial` measures mechanism 1 directly from the case geometry and the near-field DEM;
`log_summary` reads the solver's own exclusion counts. Neither changes the case.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from backend.shared.grid import FLOAT_NODATA, CanonicalGrid

from . import solver_log


def inlet_burial(case_meta: dict, grid_near: CanonicalGrid, dem_near: np.ndarray, centreline,
                 width_m: float, samples: int = 41) -> dict:
    """Bed elevation along the inlet's width line (channel normal through the inlet centre)
    against the inlet plane's bottom (`inlet.bed_z_m`) and top (`inlet.zsurf_m`)."""
    from .generator import _tangent_at_point

    inlet = case_meta["inlet"]
    tx, ty = _tangent_at_point(centreline, inlet["x_utm_m"], inlet["y_utm_m"])
    nx, ny = -ty, tx
    offsets = np.linspace(-width_m / 2, width_m / 2, samples)
    xs, ys = inlet["x_utm_m"] + offsets * nx, inlet["y_utm_m"] + offsets * ny
    cols = np.floor((xs - grid_near.origin_x) / grid_near.cell_size_m).astype(int)
    rows = np.floor((grid_near.origin_y - ys) / grid_near.cell_size_m).astype(int)
    inside = (rows >= 0) & (rows < dem_near.shape[0]) & (cols >= 0) & (cols < dem_near.shape[1])
    bed = np.full(samples, np.nan)
    bed[inside] = dem_near[rows[inside], cols[inside]]
    bed[bed == FLOAT_NODATA] = np.nan
    valid = np.isfinite(bed)
    bottom, top = float(inlet["bed_z_m"]), float(inlet["zsurf_m"])
    above_bottom = valid & (bed > bottom)
    return {"rule": "DEM sampled along the channel normal through the inlet centre",
            "width_m": width_m, "samples": samples, "valid_samples": int(valid.sum()),
            "inlet_bottom_z_m": bottom, "inlet_top_z_m": top,
            "fraction_of_width_bed_above_inlet_bottom": float(above_bottom.sum() / valid.sum()) if valid.any() else None,
            "fraction_of_width_fully_buried": float((valid & (bed >= top)).sum() / valid.sum()) if valid.any() else None,
            "max_bed_above_inlet_bottom_m": float(np.nanmax(bed - bottom)) if valid.any() else None,
            "buried_inlet_area_fraction": (float(np.nansum(np.clip(bed[valid] - bottom, 0, top - bottom))
                                                 / ((top - bottom) * valid.sum())) if valid.any() else None)}


def log_summary(log_text: str | None, max_excluded_fraction: float) -> dict:
    facts = solver_log.parse_log(log_text)
    return {"facts": facts, "retention": solver_log.particle_retention(facts, max_excluded_fraction)}


def diagnose_run(run_dir: str | Path, terrain_dir: str | Path, max_excluded_fraction: float) -> dict:
    import geopandas as gpd
    import rasterio

    run_dir, terrain_dir = Path(run_dir), Path(terrain_dir)
    case_meta = json.loads((run_dir / "case" / "case_meta.json").read_text(encoding="utf-8"))
    grid = CanonicalGrid.from_json(terrain_dir / "grid_nearfield.json")
    with rasterio.open(terrain_dir / "dem_nearfield.tif") as ds:
        dem = ds.read(1)
    centreline = gpd.read_file(terrain_dir / "centreline.gpkg").geometry.iloc[0]
    width = case_meta.get("inlet", {}).get("width_m") or case_meta["inlet"]["area_m2"] / (
        case_meta["inlet"]["zsurf_m"] - case_meta["inlet"]["bed_z_m"])
    logs = sorted(run_dir.glob("attempts/*/solver.log"))
    return {"run_dir": str(run_dir),
            "inlet_burial": inlet_burial(case_meta, grid, dem, centreline, width),
            "attempts": {p.parent.name: log_summary(p.read_text(encoding="utf-8", errors="replace"),
                                                    max_excluded_fraction) for p in logs}}


def main(argv: list[str] | None = None) -> int:
    from .settings import load_sph_settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("terrain_dir", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(diagnose_run(args.run_dir, args.terrain_dir,
                                  load_sph_settings().max_excluded_particle_fraction), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

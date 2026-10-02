"""Task D: honest content for GET /validation/{site_id}[?event=] when a site has a real
solver run but no trained M5 emulator library and no digitized observed flood extent.

Three pieces, none of them fabricated:
  1. Observed-vs-simulated extent (`observed_extent_status`) -- reports whether a digitized
     observed outline exists (`backend.m7_gee.observed`); if not, says so plainly instead of
     inventing IoU/F1 numbers.
  2. `literature_comparison` -- compares this run's own POI output at a named point against
     published reconstructions already cited in docs/data_sources.md, explicitly labelled as
     a comparison with other models, never as an observation. Values are quoted verbatim from
     that doc (the same discipline CLAUDE.md rule 4 applies to docs/equations.md) and never
     tuned or invented here.
  3. `synthetic_loocv_summary` -- surfaces the M5 emulator's synthetic-test-world LOOCV report
     (docs/m5_spec.md), clearly labelled as a synthetic world, not a site-specific validation,
     since no real per-site emulator library has been trained yet.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.m0_api import registry

# Literature reconstruction values (Feature 12: moved to config/literature_references.yaml, keyed
# by site). Point-citation display data only -- never a model input, never an observation, and
# never validation (they are modelled reconstructions).
def _literature(site_id: str) -> dict[str, Any]:
    from backend.m0_api import run_metadata
    return run_metadata.literature(site_id)


CHUNGTHANG_LITERATURE_CITATIONS: list[dict[str, Any]] = list(_literature("teesta").get("citations", []))

LITERATURE_COMPARISON_CAVEATS: list[str] = [
    "This is a comparison against other published model reconstructions, not against direct "
    "field observations of the event.",
    "The 7355 m3/s figure (src_045) was already used as this run's own upstream forcing target, "
    "so it cannot serve as an independent check on this run.",
    "This run's point-of-interest output records depth, velocity and water-surface elevation "
    "only; no downstream discharge (m3/s) is computed, so peak discharge cannot be compared "
    "directly against the m3/s citations.",
]


def _onset_year(site_id: str, event_id: str, cfg: Any | None = None) -> str:
    from backend.shared.site_config import load_site_config

    cfg = cfg or load_site_config(site_id)
    event = next((e for e in cfg.events if e.id == event_id), None)
    if event is None:
        raise ValueError(f"site '{site_id}' has no event '{event_id}'")
    return str(event.onset.value)[:4]


def resolve_observed_extent_path(site_id: str, event_id: str, data_dir: Path, cfg: Any | None = None) -> Path:
    """The canonical `data/<site_id>/observed/flood_extent_<onset_year>.geojson` path for this
    event (contract §1.8), whether or not the file exists yet."""
    from backend.m7_gee import cache as gee_cache

    onset_year = _onset_year(site_id, event_id, cfg)
    return gee_cache.observed_extent_path(site_id, onset_year, data_dir)


def observed_extent_status(site_id: str, event_id: str, data_dir: Path, cfg: Any | None = None) -> dict[str, Any]:
    """Whether a digitized observed flood extent exists for this event, at the canonical
    `data/<site_id>/observed/flood_extent_<onset_year>.geojson` path (contract §1.8,
    `backend.m7_gee.observed`). Never fabricates an extent when none has been digitized -- and
    when none exists, names the exact path an operator should place one at, rather than a vague
    "somewhere" message."""
    path = resolve_observed_extent_path(site_id, event_id, data_dir, cfg)
    if not path.is_file():
        return {
            "available": False,
            "note": "Observed flood-extent outline not yet digitized for this event. "
                    "No IoU/F1 extent comparison is possible until one is added via "
                    f"backend.m7_gee.observed, at data/{site_id}/observed/{path.name}.",
        }
    return {
        "available": True,
        "extent_url": f"/api/v1/files/{site_id}/observed/{path.name}",
    }


def build_extent_metrics(observed_path: Path, run_meta: dict[str, Any], run_dir: Path) -> dict[str, Any] | None:
    """Real IoU/F1/precision/recall of this run's simulated wet extent vs. a digitized observed
    extent, both rasterized onto this run's own `summary/max_depth.tif` grid (same wet-cell
    threshold `build_predicted_extent` uses). Returns `None` -- never a fabricated or partial
    metric -- unless the observed file, the raster and the extent threshold are all present.

    Validation only: never used for training or calibration of any model (CLAUDE.md rule 3;
    docs/handoff_contract.md §5.7)."""
    if not observed_path.is_file():
        return None
    thresholds = run_meta.get("thresholds") or {}
    extent_m = thresholds.get("extent_m")
    max_depth_tif = run_dir / "summary" / "max_depth.tif"
    if extent_m is None or not max_depth_tif.is_file():
        return None

    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    features = observed.get("features", [])
    if not features:
        return None

    import numpy as np
    import rasterio
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom

    with rasterio.open(max_depth_tif) as ds:
        arr = ds.read(1)
        simulated_wet = (arr > extent_m) & (arr != ds.nodata if ds.nodata is not None else True)
        shapes = [(transform_geom("EPSG:4326", ds.crs, f["geometry"]), 1) for f in features]
        observed_wet = rasterize(
            shapes, out_shape=ds.shape, transform=ds.transform, fill=0, dtype="uint8"
        ).astype(bool)

    tp = int(np.logical_and(simulated_wet, observed_wet).sum())
    fp = int(np.logical_and(simulated_wet, ~observed_wet).sum())
    fn = int(np.logical_and(~simulated_wet, observed_wet).sum())
    union = tp + fp + fn
    return {
        "iou": (tp / union) if union > 0 else None,
        "f1_dice": (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) > 0 else None,
        "precision": (tp / (tp + fp)) if (tp + fp) > 0 else None,
        "recall": (tp / (tp + fn)) if (tp + fn) > 0 else None,
        "basis": f"cell-wise comparison on this run's own summary/max_depth.tif grid, "
                 f"wet threshold depth > {extent_m} m",
        "validation_only": True,
    }


def _poi_arrival_and_peak(timeseries_csv: Path, poi_suffix: str) -> dict[str, Any] | None:
    """Reads M3's timeseries.csv (contract's `poi_id,t_s,depth_m,velocity_ms,wse_m,
    arrival_s_since_t0` shape) for the first POI whose id ends with `__poi__<poi_suffix>`."""
    peak_depth_m = 0.0
    peak_velocity_ms = 0.0
    arrival_s: float | None = None
    found = False
    with timeseries_csv.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if not row["poi_id"].endswith(f"__poi__{poi_suffix}"):
                continue
            found = True
            peak_depth_m = max(peak_depth_m, float(row["depth_m"]))
            peak_velocity_ms = max(peak_velocity_ms, float(row["velocity_ms"]))
            if arrival_s is None and row["arrival_s_since_t0"]:
                arrival_s = float(row["arrival_s_since_t0"])
    if not found:
        return None
    return {"peak_depth_m": peak_depth_m, "peak_velocity_ms": peak_velocity_ms,
            "arrival_s_since_t0": arrival_s}


def _t0_from_hydrograph(hydrograph_json: Path) -> tuple[datetime, dict[str, Any]] | None:
    """Derives absolute t0 (CLAUDE.md rule 6: start of the most upstream breach) from M2's
    breach-hydrograph sidecar: t0 = peak_time_ist - duration_s/2 (the hydrograph's own
    symmetric-triangular construction formula). Both inputs are already-sourced reconstruction
    targets recorded in that file; this only re-derives their implied start time, it invents
    nothing new."""
    data = json.loads(hydrograph_json.read_text())
    provenance = data.get("provenance", {})
    constraints = provenance.get("source_constraints", {})
    construction = provenance.get("construction", {})
    peak_time = constraints.get("peak_time_ist", {})
    duration_s = construction.get("duration_s")
    if not peak_time.get("value") or duration_s is None:
        return None
    t_peak = datetime.fromisoformat(peak_time["value"])
    t0 = t_peak - timedelta(seconds=duration_s / 2)
    return t0, {
        "peak_time_ist": peak_time["value"],
        "peak_time_status": peak_time.get("status"),
        "duration_s": duration_s,
    }


def build_literature_comparison(site_id: str, run_meta: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Point comparison of this run's own Chungthang POI output against literature
    reconstructions (see module docstring). `available: False` whenever the run's own outputs
    don't have what's needed -- never filled in with a guess."""
    ref = _literature(site_id)
    poi_name = ref.get("poi")
    result: dict[str, Any] = {
        "available": False,
        "kind": "literature_reconstruction_comparison",
        "is_validation": False,
        "poi": poi_name,
        "simulated": None,
        "literature": list(ref.get("citations", [])),
        "caveats": LITERATURE_COMPARISON_CAVEATS if ref else [],
    }
    if not poi_name:
        return result
    run_id = run_meta.get("run_id")
    if not run_id:
        return result
    timeseries_csv = data_dir / site_id / "runs" / run_id / "timeseries.csv"
    if not timeseries_csv.is_file():
        return result
    poi = _poi_arrival_and_peak(timeseries_csv, poi_name)
    if poi is None or poi["arrival_s_since_t0"] is None:
        return result
    simulated: dict[str, Any] = {
        "peak_depth_m": poi["peak_depth_m"],
        "peak_velocity_ms": poi["peak_velocity_ms"],
        "arrival_s_since_t0": poi["arrival_s_since_t0"],
    }
    hydrograph_rel = run_meta.get("forcing_provenance_path")
    if hydrograph_rel:
        hydrograph_path = data_dir / site_id / hydrograph_rel
        if hydrograph_path.is_file():
            t0_info = _t0_from_hydrograph(hydrograph_path)
            if t0_info is not None:
                t0, meta = t0_info
                arrival_dt = t0 + timedelta(seconds=poi["arrival_s_since_t0"])
                simulated["arrival_time_ist_estimate"] = arrival_dt.isoformat()
                simulated["t0_derivation"] = (
                    f"t0 = peak_time_ist ({meta['peak_time_ist']}, status: {meta['peak_time_status']}) "
                    f"- duration_s/2 ({meta['duration_s']}/2 s)"
                )
    result["available"] = True
    result["simulated"] = simulated
    return result


def build_predicted_extent(run_meta: dict[str, Any], run_dir: Path) -> dict[str, Any] | None:
    """Predicted flooded area from this run's own `summary/max_depth.tif`, at the run's own
    extent threshold (`run_meta.thresholds.extent_m`) -- a real computed value from the run's
    output, not an invented one. Returns None if the raster or threshold isn't available."""
    thresholds = run_meta.get("thresholds") or {}
    extent_m = thresholds.get("extent_m")
    max_depth_tif = run_dir / "summary" / "max_depth.tif"
    if extent_m is None or not max_depth_tif.is_file():
        return None
    import numpy as np
    import rasterio

    with rasterio.open(max_depth_tif) as ds:
        arr = ds.read(1)
        cell_area_m2 = abs(ds.res[0] * ds.res[1])
        if ds.nodata is not None:
            wet = (arr > extent_m) & (arr != ds.nodata)
        else:
            wet = arr > extent_m
        area_m2 = float(wet.sum()) * cell_area_m2
    return {
        "value": area_m2, "low": area_m2, "high": area_m2, "unit": "m2",
        "interval": "none", "kind": "predicted", "confidence": "LOW",
        "basis": f"cells with depth > {extent_m} m in this run's summary/max_depth.tif",
        "source": run_meta.get("run_id", ""),
    }


def synthetic_loocv_summary(report_path: Path | None = None) -> dict[str, Any] | None:
    """The M5 emulator's synthetic-test-world LOOCV report (docs/m5_spec.md), clearly labelled
    as a synthetic world -- not a site-specific validation. Returns None if the report hasn't
    been generated (e.g. `python -m backend.m5_emulator.loocv` was never run)."""
    if report_path is None:
        report_path = Path(os.environ.get(
            "SIH26_M5_SYNTHETIC_LOOCV_REPORT",
            registry.REPO_ROOT / "reports" / "m5_synthetic" / "validation" / "loocv.json",
        ))
    if not report_path.is_file():
        return None
    data = json.loads(report_path.read_text())
    return {
        "world": "synthetic_test_world",
        "note": "M5 acceptance-test result on the synthetic emulator test world (docs/m5_spec.md). "
                "Not a Teesta-specific (or any real-site) validation -- no real-site emulator "
                "library has been trained yet.",
        "model": data.get("model"),
        "n_runs": data.get("n_runs"),
        "summary": data.get("summary"),
        "baseline_linear": data.get("baseline_linear"),
        "baseline_nearest": data.get("baseline_nearest"),
        "acceptance": data.get("acceptance"),
        "grade_thresholds_ref": data.get("grade_thresholds_ref"),
    }

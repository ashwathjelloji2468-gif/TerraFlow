"""Feature 3: the breach stage's outputs for any registered site -- `breach_params.json` with
Feature 2 evidence, plus per-dam reference low/high hydrographs (docs/decisions.md 2026-10-01,
F3-D3/D5/D6).

Reference hydrographs (`breach/hydrographs/<site_id>_n_m2_low__<dam>.csv` / `_n_m2_high__`) bracket
M2's own range bounds -- they are not designed scenarios (Feature 4) and do not vary the water
volume:

- `low`  = narrow breach (B_ave low), slow failure (T_f high), Q_p low
- `high` = wide breach (B_ave high), fast failure (T_f low), Q_p high

Method: `breach_growth_weir` when the dam's weir coefficients and storage curve are sourced
(`hydrograph._weir_prereqs`); otherwise `triangular`, which needs the Q_p range. A case whose
inputs are not all available is recorded `status: blocked` with the reason, and no file is
written -- nothing is invented (CLAUDE.md rule 3). Triggered cascade dams are not given a
reference hydrograph: their breach starts when routed inflow from upstream crosses `trigger.value`
(`cascade.py`, two_stage_imposed), which needs the stage-1 hydraulic run.

Evidence (F3-D5): what Feature 2 produced for the site -- selected DEM, lake/reservoir extent from
`terrain/water_mask.tif`, M7's latest lake outline, the HydroBASINS catchment and the GloFAS
discharge summary -- is recorded in `breach_params.provenance.evidence` as context only. None of it
changes an equation input; GloFAS is not used as base flow (F3-D6).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np

from backend.shared.site_config import Dam, SiteConfig, default_data_dir

from .breach_params import compute_breach_params, write_breach_params
from .hydrograph import HydrographBlocked, _weir_prereqs, hydrograph_for_dam, write_hydrograph

REFERENCE_CASES = {
    "low": {"breach_width_m": "low", "failure_time_s": "high", "peak_discharge_m3s": "low"},
    "high": {"breach_width_m": "high", "failure_time_s": "low", "peak_discharge_m3s": "high"},
}

Event = Callable[[str], None]


def reference_scenario_id(site_id: str, case: str) -> str:
    """`<site_id>_n_m2_<case>` -- the `n_<name>` (named extra) id form, docs/decisions.md."""
    return f"{site_id}_n_m2_{case}"


# --- evidence ------------------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def breach_evidence(site_id: str, data_dir: Path) -> dict:
    """Feature 2 context for the breach stage. Every item is `available` or `unavailable`."""
    site = Path(data_dir) / site_id
    out: dict = {"note": "context only: no item below is used as an equation input or as base flow"}

    terrain = _read_json(site / "terrain" / "provenance.json")
    if terrain:
        selection = terrain.get("dem_selection") or {}
        out["dem"] = {"status": "available", "product": (terrain.get("dem") or {}).get("product"),
                      "vertical_datum": terrain.get("vertical_datum"),
                      "selection_rule_met": selection.get("threshold_met"), "file": "terrain/provenance.json"}
    else:
        out["dem"] = {"status": "unavailable", "reason": "terrain/provenance.json not written yet"}

    mask_path, grid_path = site / "terrain" / "water_mask.tif", site / "terrain" / "grid.json"
    if mask_path.is_file() and grid_path.is_file():
        import rasterio

        with rasterio.open(mask_path) as ds:
            mask = ds.read(1)
            cell_area = abs(ds.transform.a * ds.transform.e)
        out["water_mask"] = {"status": "available", "file": "terrain/water_mask.tif",
                             "lake_area_m2": float(np.sum(mask == 1) * cell_area),
                             "reservoir_area_m2": float(np.sum(mask == 2) * cell_area),
                             "basis": "ESA WorldCover water class (or M7 outline) on the far-field grid"}
    else:
        out["water_mask"] = {"status": "unavailable", "reason": "terrain/water_mask.tif not written yet"}

    lake = _read_json(site / "gee" / "lake_latest.geojson")
    feats = (lake or {}).get("features") or []
    if feats:
        props = feats[0].get("properties", {})
        out["lake_latest"] = {"status": "available", "file": "gee/lake_latest.geojson",
                              "area_m2": props.get("area_m2"), "date": props.get("date"), "method": props.get("method")}
    else:
        out["lake_latest"] = {"status": "unavailable", "reason": "no gee/lake_latest.geojson outline"}

    raw = _read_json(site / "raw" / "provenance.json") or {}
    hb = raw.get("hydrobasins_catchment")
    out["hydrobasins_catchment"] = (
        {"status": "available", "file": "raw/hydrobasins_catchment.geojson", "upstream_area_m2": hb.get("upstream_area_m2"),
         "n_basins": hb.get("n_basins"), "method": hb.get("method")}
        if hb and hb.get("status") == "fetched" else
        {"status": "unavailable", "reason": (hb or {}).get("reason", "not ingested")})
    q = raw.get("discharge_glofas")
    out["discharge_glofas"] = (
        {"status": "available", "file": "raw/discharge_glofas.csv", "kind": q.get("kind"),
         "summary_m3s": q.get("summary_m3s"), "period": q.get("period"), "point_basis": q.get("point_basis")}
        if q and q.get("status") == "fetched" else
        {"status": "unavailable", "reason": (q or {}).get("reason", "not ingested")})
    return out


# --- reference hydrographs -----------------------------------------------------------------------
def _case_params(dam: Dam, entry: dict, case: str) -> tuple[dict | None, str | None, str | None]:
    """(params, method, None) for one reference case, or (None, None, reason) if blocked."""
    if dam.triggered_by is not None:
        return None, None, (f"triggered cascade dam (triggered_by '{dam.triggered_by}'): its breach starts when routed "
                            "inflow crosses trigger.value, which needs the stage-1 hydraulic run")
    volume = dam.breach_inputs.water_volume_above_invert.value
    if volume is None:
        return None, None, "breach_inputs.water_volume_above_invert is a placeholder (null)"
    rng = entry["parameters"]
    pick = REFERENCE_CASES[case]
    for output in ("breach_width_m", "failure_time_s"):
        if rng[output].get("status") == "blocked" or rng[output].get(pick[output]) is None:
            return None, None, f"{output} range is blocked: {rng[output].get('reason')}"
    params = {"water_volume_m3": float(volume),
              "breach_width_m": float(rng["breach_width_m"][pick["breach_width_m"]]),
              "failure_time_s": float(rng["failure_time_s"][pick["failure_time_s"]])}
    curve, _, weir_reason = _weir_prereqs(dam)
    if curve is not None:
        return {**params, "method": "breach_growth_weir"}, "breach_growth_weir", None
    qp = rng["peak_discharge_m3s"]
    if qp.get("status") == "blocked" or qp.get(pick["peak_discharge_m3s"]) is None:
        return None, None, (f"weir method unavailable ({weir_reason}) and the peak_discharge_m3s range is blocked: "
                            f"{qp.get('reason')}")
    return ({**params, "peak_discharge_m3s": float(qp[pick["peak_discharge_m3s"]]), "method": "triangular"},
            "triangular", None)


def write_reference_hydrographs(cfg: SiteConfig, payload: dict, data_dir: Path, event: Event) -> list[dict]:
    """Write every computable reference hydrograph; return one record per dam and case."""
    records = []
    entries = {e["dam_id"]: e for e in payload["dams"]}
    for dam in cfg.dams:
        for case in REFERENCE_CASES:
            scenario_id = reference_scenario_id(cfg.site.id, case)
            record = {"dam_id": dam.id, "case": case, "scenario_id": scenario_id}
            params, method, reason = _case_params(dam, entries[dam.id], case)
            if params is not None:
                try:
                    hg = hydrograph_for_dam(dam, params)
                    csv_path, json_path = write_hydrograph(hg, cfg.site.id, scenario_id, data_dir=data_dir)
                except (HydrographBlocked, ValueError) as e:
                    reason = str(getattr(e, "reason", e))
                else:
                    base = Path(data_dir) / cfg.site.id
                    record.update({"status": "written", "method": method,
                                   "csv": str(csv_path.relative_to(base)), "sidecar": str(json_path.relative_to(base)),
                                   "params": {k: v for k, v in params.items() if k != "method"},
                                   "peak_q_m3s": hg.peak_q_m3s, "volume_m3": hg.volume_m3,
                                   "mass_balance_error_pct": hg.mass_balance_error_pct,
                                   "has_placeholders": hg.has_placeholders})
                    event(f"breach {dam.id} {case}: {method} hydrograph written (peak {hg.peak_q_m3s:.4g} m3/s)")
            if "status" not in record:
                record.update({"status": "blocked", "reason": reason})
                event(f"breach {dam.id} {case}: reference hydrograph blocked ({reason})")
            records.append(record)
    return records


def run_breach_stage(cfg: SiteConfig, data_dir: Path | None = None, event: Event | None = None) -> Path:
    """The onboarding `breach` stage: compute M2 parameters for every dam, write the reference
    hydrographs, then `breach_params.json` (schema-validated) with evidence + hydrograph records."""
    data_dir = Path(data_dir or default_data_dir())
    event = event or (lambda msg: None)
    payload = compute_breach_params(cfg)
    for entry in payload["dams"]:
        parts = []
        for output, value in entry["parameters"].items():
            parts.append(f"{output} blocked" if value.get("status") == "blocked"
                         else f"{output} {value['low']:.4g}-{value['high']:.4g}")
        event(f"breach {entry['dam_id']}: " + "; ".join(parts))
    payload["provenance"]["evidence"] = breach_evidence(cfg.site.id, data_dir)
    payload["provenance"]["reference_hydrographs"] = write_reference_hydrographs(cfg, payload, data_dir, event)
    payload["provenance"]["data_sources"] = sorted(set(payload["provenance"]["data_sources"]) | {"docs/decisions.md"})
    return write_breach_params(cfg, data_dir=data_dir, payload=payload)

"""Canonical M3 routed-discharge artifact writer.

The writer validates/records discharge extracted from a solver run. Selecting the section and
flow-integration method remains explicit caller input; this module does not infer either.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from jsonschema import validate


SCHEMA = Path(__file__).resolve().parents[2] / "contracts/schemas/routed_discharge.schema.json"


def write_routed_discharge(output_dir: str | Path, *, site_id: str, scenario_id: str,
                           source_run_id: str, t_s, q_m3s,
                           routing_method: str, section: dict,
                           provenance: dict, validation_status: str = "artifact_schema_checked") -> tuple[Path, Path]:
    """Write `timeseries.csv` and provenance sidecar after extraction from an M3 result.

    `section` must name the cross-section geometry/CRS and `provenance` the source map/history
    file(s). No discharge is synthesized here.
    """
    t = np.asarray(t_s, dtype=float)
    q = np.asarray(q_m3s, dtype=float)
    if t.ndim != 1 or t.shape != q.shape or len(t) < 2 or not np.isfinite(t).all() or not np.isfinite(q).all():
        raise ValueError("routed discharge requires at least two finite, matching time and flow values")
    if np.any(np.diff(t) <= 0) or np.any(q < 0):
        raise ValueError("routed discharge timestamps must increase and flow must be nonnegative")
    if not routing_method.strip() or not section or not provenance:
        raise ValueError("routing method, section geometry, and source provenance are required")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    csv_path, sidecar_path = root / "timeseries.csv", root / "routed_discharge.json"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["t_s", "q_m3s"])
        writer.writerows(zip(t.tolist(), q.tolist()))
    record = {
        "contract_version": "0.3.0", "site_id": site_id, "scenario_id": scenario_id,
        "source_m3_run_id": source_run_id, "timeseries": "timeseries.csv",
        "time_column": "t_s", "flow_column": "q_m3s", "time_unit": "s",
        "flow_unit": "m3/s", "routing_method": routing_method, "section": section,
        "provenance": provenance, "validation_status": validation_status,
    }
    validate(record, json.loads(SCHEMA.read_text()))
    sidecar_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return csv_path, sidecar_path


def read_routed_discharge(sidecar_path: str | Path, *, site_id: str | None = None,
                          scenario_id: str | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """Load/validate a routed series for M4; reject cross-site/scenario artifacts."""
    sidecar_path = Path(sidecar_path)
    record = json.loads(sidecar_path.read_text(encoding="utf-8"))
    validate(record, json.loads(SCHEMA.read_text()))
    if site_id is not None and record["site_id"] != site_id:
        raise ValueError(f"routed discharge belongs to site {record['site_id']!r}, not {site_id!r}")
    if scenario_id is not None and record["scenario_id"] != scenario_id:
        raise ValueError("routed discharge scenario does not match requested M4 scenario")
    csv_path = (sidecar_path.parent / record["timeseries"]).resolve()
    if csv_path.parent != sidecar_path.parent.resolve() or not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise ValueError("routed discharge timeseries is missing or escapes its artifact directory")
    data = np.genfromtxt(csv_path, delimiter=",", names=True)
    t, q = np.atleast_1d(data["t_s"]).astype(float), np.atleast_1d(data["q_m3s"]).astype(float)
    if len(t) < 2 or not np.isfinite(t).all() or not np.isfinite(q).all() or np.any(np.diff(t) <= 0) or np.any(q < 0):
        raise ValueError("routed discharge timeseries is malformed")
    return t, q, record

"""Feature 16 P5: reviewable GloFAS evidence for `domains.far_field.inflow.base_flow` -- any site.

GloFAS v4 daily discharge (docs/data_sources.md src_075) is MODELLED, not gauged. This module never
writes a site config and never picks a number: it turns the series `backend.m1_terrain.discharge`
already fetched (`data/<site>/raw/discharge_glofas.csv` + its `raw/provenance.json` entry) into one
pending CANDIDATE record whose `proposed_value` is null and which carries the summary statistics
discharge.py already defines, re-computed from the CSV and checked against the recorded ones. A named
reviewer chooses the statistic (team decision 2026-10-03, Feature 16 P5: "no single value").

Outcomes (vocabulary shared with input_resolution / data_foundation):
- CANDIDATE     valid local series sampled at the CONFIGURED inflow location -> pending record
- UNRESOLVED    inflow location missing/placeholder, or the series was sampled elsewhere (dam fallback)
- UNVERIFIABLE  no fetched provenance entry, CSV missing/unreadable, or units not m^3/s
- MISMATCH      CSV hash, request point or recorded statistics disagree with the provenance

Only CANDIDATE writes `data/<site>/raw/base_flow_evidence.json` (deterministic, no new timestamps);
every other outcome writes nothing and keeps any existing file. The configured base_flow is quoted,
never changed. CLI: python -m backend.m1_terrain.base_flow_candidates <site_id> [--data-dir] [--write]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path

from backend.m1_terrain import discharge
from backend.shared.input_resolution import CANDIDATE, UNRESOLVED
from backend.shared.site_config import SITE_ID_PATTERN, SiteConfig, default_data_dir

UNVERIFIABLE, MISMATCH = "UNVERIFIABLE", "MISMATCH"  # same words as data_foundation file checks
PENDING = "pending"
TARGET = "domains.far_field.inflow.base_flow"
INFLOW = "domains.far_field.inflow.location"
OUTPUT_NAME = "base_flow_evidence.json"
GENERATOR, GENERATOR_VERSION = "backend.m1_terrain.base_flow_candidates", 1
UNIT = "m^3/s"
STAT_KEYS = ("min", "p10", "median", "mean", "p90", "max")
STATISTIC_DEFINITIONS = {
    "population": "every non-missing daily value in the fetched period (`period`), as discharge.py computes summary_m3s",
    "min": "minimum", "max": "maximum", "mean": "arithmetic mean (statistics.fmean)",
    "median": "median (statistics.median)",
    "p10": "10th percentile, linear interpolation between closest ranks (discharge._percentile)",
    "p90": "90th percentile, linear interpolation between closest ranks (discharge._percentile)",
}
CAVEATS = (
    "GloFAS v4 discharge is modelled (reanalysis, then archived forecasts), not gauged or observed.",
    "~0.05 deg (~5 km) grid: the returned grid point may differ from the requested inflow location.",
    "docs/data_sources.md src_075: verify the GloFAS (CC BY 4.0) and Open-Meteo licence pages before "
    "setting any derived value to status: sourced.",
    "No statistic is proposed: the team chooses which (if any) represents base flow before t0.",
)


class UnsafeEvidencePath(ValueError):
    """A path would leave `<data_dir>/<site_id>/`."""


def site_paths(site_id: str, data_dir: Path) -> dict[str, Path]:
    if not isinstance(site_id, str) or not re.fullmatch(SITE_ID_PATTERN, site_id):
        raise UnsafeEvidencePath(f"unsafe or invalid site id {site_id!r}")
    raw = Path(data_dir) / site_id / "raw"
    paths = {"provenance": raw / "provenance.json", "csv": raw / discharge.OUTPUT_NAME, "output": raw / OUTPUT_NAME}
    root = (Path(data_dir) / site_id).resolve()
    for key, p in paths.items():
        if not p.resolve().is_relative_to(root):
            raise UnsafeEvidencePath(f"{key} path resolves outside data/{site_id}/ (symlink escape)")
    return paths


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stats(values: list[float]) -> dict:
    """discharge.py's summary_m3s, recomputed with the same functions."""
    return {"min": min(values), "p10": discharge._percentile(values, 0.10), "median": statistics.median(values),
            "mean": statistics.fmean(values), "p90": discharge._percentile(values, 0.90), "max": max(values)}


def _configured(cfg: SiteConfig) -> dict | None:
    bf = cfg.domains.far_field.inflow.base_flow
    return None if bf is None else {"value": bf.value, "unit": bf.unit, "status": bf.status, "source": bf.source}


def evaluate(cfg: SiteConfig, *, data_dir: str | Path | None = None) -> dict:
    """The evidence record for `cfg` (module docstring). Read-only."""
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    site_id = cfg.site.id
    paths = site_paths(site_id, data_dir)
    base = {"site_id": site_id, "target": TARGET, "generator": GENERATOR, "generator_version": GENERATOR_VERSION,
            "configured_base_flow": _configured(cfg), "modelled_not_gauged": True}

    def outcome(status, reason, **extra):
        return {**base, "status": status, "reason": reason, **extra}

    loc = cfg.domains.far_field.inflow.location
    if loc.value is None:
        return outcome(UNRESOLVED, f"{INFLOW} is missing (null): GloFAS is never sampled elsewhere instead")
    if loc.status != "sourced":
        return outcome(UNRESOLVED, f"{INFLOW} is a placeholder: resolve the inflow location first")
    if not paths["provenance"].is_file():
        return outcome(UNVERIFIABLE, "raw/provenance.json is not present")
    try:
        entry = (json.loads(paths["provenance"].read_text(encoding="utf-8")) or {}).get("discharge_glofas")
    except ValueError:
        return outcome(UNVERIFIABLE, "raw/provenance.json is unreadable")
    if not isinstance(entry, dict) or entry.get("status") != "fetched":
        why = (entry or {}).get("reason") if isinstance(entry, dict) else None
        return outcome(UNVERIFIABLE, "no fetched GloFAS series recorded" + (f" ({why})" if why else ""))
    if entry.get("dataset") != discharge.DATASET or entry.get("file") != discharge.OUTPUT_NAME:
        return outcome(UNVERIFIABLE, "the discharge_glofas provenance entry is not a GloFAS series written by discharge.py")
    if entry.get("units") != UNIT:
        return outcome(UNVERIFIABLE, f"recorded units {entry.get('units')!r} are not {UNIT}")
    if entry.get("point_basis") != INFLOW:
        return outcome(UNRESOLVED, f"series was sampled at {entry.get('point_basis')!r}, not at {INFLOW}")
    requested = entry.get("request_point_lonlat") or []
    if len(requested) != 2 or any(not math.isclose(float(a), float(b), abs_tol=1e-9) for a, b in zip(requested, loc.value)):
        return outcome(MISMATCH, f"series was requested at {requested}, configured inflow is {list(loc.value)}")
    if not paths["csv"].is_file():
        return outcome(UNVERIFIABLE, f"raw/{discharge.OUTPUT_NAME} is not present")
    sha = _sha256(paths["csv"])
    if not entry.get("sha256") or sha != entry["sha256"]:
        return outcome(MISMATCH, f"raw/{discharge.OUTPUT_NAME} sha256 differs from raw/provenance.json")
    try:
        with open(paths["csv"], newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        values = [float(r["river_discharge_m3s"]) for r in rows if r.get("river_discharge_m3s") not in ("", None)]
    except (OSError, KeyError, ValueError):
        return outcome(UNVERIFIABLE, f"raw/{discharge.OUTPUT_NAME} is unreadable")
    if not values:
        return outcome(UNVERIFIABLE, f"raw/{discharge.OUTPUT_NAME} has no valid values")
    stats = _stats(values)
    recorded = entry.get("summary_m3s") or {}
    if any(k not in recorded or not math.isclose(stats[k], float(recorded[k]), rel_tol=1e-9, abs_tol=1e-12)
           for k in STAT_KEYS):
        return outcome(MISMATCH, "statistics recomputed from the CSV differ from summary_m3s in raw/provenance.json")
    keep = ("dataset", "source", "service_url", "license", "kind", "period", "n_days", "n_missing", "time_basis",
            "request_point_lonlat", "returned_point_lonlat", "point_basis", "fetched_at", "size_bytes")
    return {
        **base, "status": CANDIDATE, "review_state": PENDING, "proposed_value": None, "unit": UNIT,
        "reason": "modelled GloFAS statistics for review; no value proposed",
        "rationale": "A named reviewer must decide whether any statistic represents the steady channel flow before "
                     "t0 for this site and event; until then base_flow keeps its configured value and status.",
        "statistics_m3s": {k: stats[k] for k in STAT_KEYS},
        "statistic_definitions": STATISTIC_DEFINITIONS,
        "evidence": {"file": f"raw/{discharge.OUTPUT_NAME}", "sha256": sha, "file_check": "VERIFIED",
                     **{k: entry[k] for k in keep if k in entry}},
        "caveats": list(CAVEATS),
    }


def write(record: dict, *, data_dir: str | Path | None = None) -> tuple[Path, bool]:
    """Write a CANDIDATE record atomically; identical content is not rewritten. Refuses other statuses."""
    if record.get("status") != CANDIDATE:
        raise ValueError(f"only a {CANDIDATE} record is written, got {record.get('status')!r}")
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    out = site_paths(record["site_id"], data_dir)["output"]
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if out.is_file() and out.read_text(encoding="utf-8") == text:
        return out, False
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return out, True


def run(cfg: SiteConfig, *, data_dir: str | Path | None = None, write_output: bool = True) -> dict:
    record = evaluate(cfg, data_dir=data_dir)
    written = False
    if write_output and record["status"] == CANDIDATE:
        _, written = write(record, data_dir=data_dir)
    return {"site_id": cfg.site.id, "status": record["status"], "reason": record["reason"], "written": written,
            "record": record}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pending GloFAS base-flow evidence (modelled; never applied)")
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--write", action="store_true", help="write raw/base_flow_evidence.json (default: dry run)")
    args = parser.parse_args(argv)
    from backend.m3_dflowfm.preflight import _load_config
    data_dir = args.data_dir or default_data_dir()
    try:
        site_paths(args.site_id, data_dir)
        cfg, _ = _load_config(args.site_id, Path(data_dir), None)
    except Exception as e:  # noqa: BLE001
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    out = run(cfg, data_dir=data_dir, write_output=args.write)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["status"] == CANDIDATE else 1


if __name__ == "__main__":
    sys.exit(main())

"""Canonical M3 routed-discharge artifact (contract 0.3.0, §4.4): writer, validator, reader.

The writer records discharge extracted from a solver run. Selecting the section and the
flow-integration method remains explicit caller input; this module does not infer either and never
synthesizes discharge.

Feature 17 additions (decisions E1/E2, no schema change):

- **Integrity.** `provenance.timeseries_sha256` is the sha256 of `timeseries.csv`;
  `provenance.caveats` lists the artifact's caveat IDs/texts. Both live inside the schema's free-form
  `provenance` object.
- **Atomic replacement.** Both files are written into a staging directory beside the target,
  validated there, and only then swapped in (directory rename). A failed or interrupted write leaves
  the previous artifact untouched; a crash between the two renames leaves the previous artifact
  under `.<name>.prev-*` and the new one complete -- never a mixed pair. Unrelated sibling files in
  an existing artifact directory are carried over.
- **Validation** (`validate_routed_discharge`): schema, allowed `validation_status`, CSV header,
  hash, finite / strictly increasing / non-negative series, section geometry, CRS, provenance
  completeness, and (optionally) time coverage and the source M3 run. Every failure raises
  `RoutedDischargeInvalid`, a `ValueError`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import uuid
from pathlib import Path

import numpy as np
from jsonschema import ValidationError, validate

SCHEMA = Path(__file__).resolve().parents[2] / "contracts/schemas/routed_discharge.schema.json"
TIMESERIES, MANIFEST = "timeseries.csv", "routed_discharge.json"
ARTIFACT_DIRNAME = "routed_discharge"  # contract §4.4: runs/<m3_run_id>/routed_discharge/

#: A manifest in one of these states may feed an M4 case. `blocked` / `failed` never may.
USABLE_STATUSES = ("artifact_schema_checked", "artifact_validated")
#: Production consumption additionally requires the scientifically validated state (decision S4 is
#: still open, so nothing in this repository produces it yet).
PRODUCTION_STATUSES = ("artifact_validated",)
REQUIRED_PROVENANCE = ("timeseries_sha256", "source_map")

_CRS_RE = re.compile(r"^EPSG:(\d+)$")
_SCENARIO_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_RUN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}__(delft3d|sph)$")


class RoutedDischargeInvalid(ValueError):
    """The routed-discharge artifact failed a deterministic check (message names the check)."""


def check_scenario_id(value: str, what: str = "scenario_id") -> str:
    """IDs that become path components are restricted to the contract's slug alphabet."""
    if not isinstance(value, str) or not _SCENARIO_ID_RE.fullmatch(value):
        raise RoutedDischargeInvalid(f"{what} {value!r} is not a valid identifier")
    return value


def check_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise RoutedDischargeInvalid(f"run id {value!r} is not a valid <scenario_id>__<model> identifier")
    return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def crs_epsg(crs: str) -> int:
    match = _CRS_RE.fullmatch(str(crs or ""))
    if not match:
        raise RoutedDischargeInvalid(f"section CRS {crs!r} is not of the form 'EPSG:<code>'")
    return int(match.group(1))


def _check_series(t: np.ndarray, q: np.ndarray) -> None:
    if t.ndim != 1 or t.shape != q.shape or len(t) < 2 or not np.isfinite(t).all() or not np.isfinite(q).all():
        raise RoutedDischargeInvalid("routed discharge requires at least two finite, matching time and flow values")
    if np.any(np.diff(t) <= 0) or np.any(q < 0):
        raise RoutedDischargeInvalid("routed discharge timestamps must increase and flow must be nonnegative")


def check_section(section: dict, expected_epsg: int | None = None) -> int:
    """A CRS-qualified LineString with >= 2 finite points and non-zero length; returns its EPSG."""
    if not isinstance(section, dict) or section.get("type") != "LineString":
        raise RoutedDischargeInvalid("section must be a LineString object")
    coords = section.get("coordinates") or []
    try:
        pts = np.asarray([[float(c[0]), float(c[1])] for c in coords], dtype=float)
    except (TypeError, ValueError, IndexError) as exc:
        raise RoutedDischargeInvalid(f"section coordinates are malformed: {exc}") from None
    if len(pts) < 2 or not np.isfinite(pts).all():
        raise RoutedDischargeInvalid("section needs at least two finite coordinates")
    length = float(np.sum(np.hypot(*np.diff(pts, axis=0).T)))
    if not math.isfinite(length) or length <= 0:
        raise RoutedDischargeInvalid("section has zero length")
    epsg = crs_epsg(section.get("crs"))
    if expected_epsg is not None and epsg != int(expected_epsg):
        raise RoutedDischargeInvalid(f"section CRS EPSG:{epsg} does not match the expected EPSG:{int(expected_epsg)}")
    return epsg


def _swap_in(staging: Path, target: Path) -> None:
    """Replace `target` with the fully written `staging` directory without ever leaving a mixed pair."""
    if target.exists():
        for child in target.iterdir():  # keep unrelated files (e.g. m4_inlet.json) beside the artifact
            if child.name not in (TIMESERIES, MANIFEST) and not (staging / child.name).exists():
                if child.is_dir():
                    shutil.copytree(child, staging / child.name)
                else:
                    shutil.copy2(child, staging / child.name)
        previous = target.parent / f".{target.name}.prev-{uuid.uuid4().hex[:12]}"
        target.rename(previous)
        try:
            staging.rename(target)
        except Exception:
            previous.rename(target)  # restore the last valid artifact
            raise
        shutil.rmtree(previous, ignore_errors=True)
    else:
        staging.rename(target)


def write_routed_discharge(output_dir: str | Path, *, site_id: str, scenario_id: str,
                           source_run_id: str, t_s, q_m3s,
                           routing_method: str, section: dict,
                           provenance: dict, validation_status: str = "artifact_schema_checked",
                           caveats: list | None = None) -> tuple[Path, Path]:
    """Write `timeseries.csv` + `routed_discharge.json` into `output_dir` atomically.

    `section` must name the cross-section geometry/CRS and `provenance` the source map/history
    file(s). The CSV sha256 and `caveats` are recorded in `provenance`. No discharge is synthesized.
    """
    t = np.asarray(t_s, dtype=float)
    q = np.asarray(q_m3s, dtype=float)
    _check_series(t, q)
    if not routing_method.strip() or not section or not provenance:
        raise RoutedDischargeInvalid("routing method, section geometry, and source provenance are required")
    check_section(section)
    target = Path(output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{uuid.uuid4().hex[:12]}"
    staging.mkdir()
    try:
        with (staging / TIMESERIES).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["t_s", "q_m3s"])
            writer.writerows(zip(t.tolist(), q.tolist()))
        prov = dict(provenance)
        prov["timeseries_sha256"] = sha256_file(staging / TIMESERIES)
        prov["caveats"] = list(caveats if caveats is not None else prov.get("caveats") or [])
        record = {
            "contract_version": "0.3.0", "site_id": site_id, "scenario_id": scenario_id,
            "source_m3_run_id": source_run_id, "timeseries": TIMESERIES,
            "time_column": "t_s", "flow_column": "q_m3s", "time_unit": "s",
            "flow_unit": "m3/s", "routing_method": routing_method, "section": section,
            "provenance": prov, "validation_status": validation_status,
        }
        validate(record, json.loads(SCHEMA.read_text()))
        (staging / MANIFEST).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        validate_routed_discharge(staging / MANIFEST, allowed_statuses=None)  # re-read what was written
        _swap_in(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target / TIMESERIES, target / MANIFEST


def validate_routed_discharge(sidecar_path: str | Path, *, site_id: str | None = None,
                              scenario_id: str | None = None, expected_epsg: int | None = None,
                              allowed_statuses: tuple[str, ...] | None = USABLE_STATUSES,
                              window_s: tuple[float, float | None] | None = None,
                              runs_dir: str | Path | None = None,
                              source_run_statuses: tuple[str, ...] | None = None
                              ) -> tuple[np.ndarray, np.ndarray, dict, dict]:
    """Every deterministic check on one artifact. Returns `(t_s, q_m3s, record, report)`.

    - `allowed_statuses=None` skips the status check (only the writer uses this, on its own output).
    - `window_s=(start, end)` requires the series to cover `[start, end]` (end None = no end check).
    - `runs_dir` requires `runs_dir/<source_m3_run_id>/run_meta.json` to exist; with
      `source_run_statuses` its classification (`routed_source_class`) must be one of them.
    """
    sidecar_path = Path(sidecar_path)
    if not sidecar_path.is_file():
        raise RoutedDischargeInvalid(f"routed discharge manifest missing: {sidecar_path.name}")
    try:
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RoutedDischargeInvalid(f"routed discharge manifest is not JSON: {exc}") from None
    try:
        validate(record, json.loads(SCHEMA.read_text()))
    except ValidationError as exc:
        raise RoutedDischargeInvalid(f"routed discharge manifest violates the schema: {exc.message}") from None
    if site_id is not None and record["site_id"] != site_id:
        raise RoutedDischargeInvalid(f"routed discharge belongs to site {record['site_id']!r}, not {site_id!r}")
    if scenario_id is not None and record["scenario_id"] != scenario_id:
        raise RoutedDischargeInvalid("routed discharge scenario does not match requested M4 scenario")
    if allowed_statuses is not None and record["validation_status"] not in allowed_statuses:
        raise RoutedDischargeInvalid(f"routed discharge validation_status {record['validation_status']!r} "
                                     f"is not usable here (allowed: {', '.join(allowed_statuses)})")
    prov = record["provenance"]
    missing = [k for k in REQUIRED_PROVENANCE if not prov.get(k)]
    if missing:
        raise RoutedDischargeInvalid("routed discharge provenance incomplete: missing " + ", ".join(missing))
    epsg = check_section(record["section"], expected_epsg)
    csv_path = (sidecar_path.parent / record["timeseries"]).resolve()
    if csv_path.parent != sidecar_path.parent.resolve() or not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise RoutedDischargeInvalid("routed discharge timeseries is missing or escapes its artifact directory")
    actual = sha256_file(csv_path)
    if actual != prov["timeseries_sha256"]:
        raise RoutedDischargeInvalid("routed discharge timeseries sha256 does not match its manifest")
    with csv_path.open(encoding="utf-8") as stream:
        header = stream.readline().strip()
    if header != "t_s,q_m3s":
        raise RoutedDischargeInvalid(f"routed discharge timeseries header {header!r} is not 't_s,q_m3s'")
    data = np.genfromtxt(csv_path, delimiter=",", names=True)
    t, q = np.atleast_1d(data["t_s"]).astype(float), np.atleast_1d(data["q_m3s"]).astype(float)
    try:
        _check_series(t, q)
    except RoutedDischargeInvalid as exc:
        raise RoutedDischargeInvalid(f"routed discharge timeseries is malformed: {exc}") from None
    report = {"manifest": str(sidecar_path), "manifest_sha256": sha256_file(sidecar_path),
              "timeseries_sha256": actual, "validation_status": record["validation_status"],
              "section_epsg": epsg, "n_samples": int(len(t)), "t_range_s": [float(t[0]), float(t[-1])],
              "checks": ["schema", "status", "provenance", "section", "crs", "hash", "header", "series"]}
    if window_s is not None:
        start, end = window_s
        if t[0] > start or (end is not None and t[-1] < end):
            raise RoutedDischargeInvalid(f"routed discharge covers [{t[0]}, {t[-1]}] s, not the required "
                                         f"window [{start}, {end if end is not None else 'end'}] s")
        report["checks"].append("time_coverage")
    if runs_dir is not None:
        source = check_run_id(record["source_m3_run_id"])
        meta_path = Path(runs_dir) / source / "run_meta.json"
        if not meta_path.is_file():
            raise RoutedDischargeInvalid(f"source M3 run {source} has no run_meta.json")
        source_class = routed_source_class(json.loads(meta_path.read_text(encoding="utf-8")))
        if source_run_statuses is not None and source_class not in source_run_statuses:
            raise RoutedDischargeInvalid(f"source M3 run {source} is {source_class}, not one of "
                                         f"{', '.join(source_run_statuses)}")
        report["source_run_class"] = source_class
        report["checks"].append("source_run")
    return t, q, record, report


def read_routed_discharge(sidecar_path: str | Path, *, site_id: str | None = None,
                          scenario_id: str | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """Load/validate a routed series for M4; reject cross-site/scenario, tampered, blocked or failed
    artifacts (`validate_routed_discharge` with its defaults)."""
    t, q, record, _ = validate_routed_discharge(sidecar_path, site_id=site_id, scenario_id=scenario_id)
    return t, q, record


def routed_source_class(run_meta: dict) -> str:
    """How an M3 run may be used as a routed-discharge source (Feature 17, audit A4).

    `run_meta.json` keeps `status: postprocessed` even for FAILED_ACCEPTANCE runs, so the status
    alone is never trusted: ACCEPTED needs `acceptance.status == ACCEPTED`; PILOT is an explicitly
    registered pilot run (`run_class == "pilot"`, controlled use only)."""
    acceptance = (run_meta.get("acceptance") or {}).get("status")
    if run_meta.get("status") != "postprocessed":
        return "NOT_POSTPROCESSED"
    if acceptance == "ACCEPTED":
        return "ACCEPTED"
    if run_meta.get("run_class") == "pilot":
        return "PILOT"
    if acceptance == "FAILED_ACCEPTANCE":
        return "FAILED_ACCEPTANCE"
    return "NOT_EVALUATED"

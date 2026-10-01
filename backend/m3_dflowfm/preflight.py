"""Read-only D-Flow FM readiness preflight (Feature 5, phase 1).

Every check reports PASS, WARNING or BLOCKED with the evidence it looked at. The preflight never
writes, creates or repairs anything, and it never supplies a value that is missing: a null
`base_flow` is BLOCKED and stays null (no default, no GloFAS inference, no MVP or pilot number).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import platform
import shutil
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.shared.site_config import default_data_dir, load_site_config, onboarded_config_path

PASS, WARNING, BLOCKED = "PASS", "WARNING", "BLOCKED"
DOCUMENTED_KERNEL = Path("~/delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh")
TIME_BINARY = Path("/usr/bin/time")
# The generator reads these; post-processing needs grid.json and domain_mask.tif.
TERRAIN_FILES = ("dem.tif", "roughness.tif", "domain.gpkg", "pois.gpkg", "centreline.gpkg",
                 "grid.json", "domain_mask.tif")
# (import name, pinned version or None) -- pins from CLAUDE.md "Stack".
PYTHON_MODULES = (("hydrolib.core", "1.4.0"), ("meshkernel", "8.3.0"), ("xarray", None),
                  ("netCDF4", None), ("rasterio", None), ("geopandas", None), ("shapely", None),
                  ("pyproj", None), ("scipy", None), ("jsonschema", None))
VERSION_DISTRIBUTIONS = {"hydrolib.core": "hydrolib-core", "meshkernel": "meshkernel"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: dict = field(default_factory=dict)


def _kernel(kernel: str | Path | None) -> tuple[Path, str]:
    if kernel is not None:
        return Path(kernel).expanduser(), "argument"
    if os.environ.get("SIH26_DFLOWFM_KERNEL"):
        return Path(os.environ["SIH26_DFLOWFM_KERNEL"]).expanduser(), "SIH26_DFLOWFM_KERNEL"
    return DOCUMENTED_KERNEL.expanduser(), "documented default (CLAUDE.md)"


def check_platform() -> Check:
    system, machine = platform.system(), platform.machine().lower()
    ev = {"system": system, "machine": machine}
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return Check("platform", PASS, "Linux x86_64 (the kernel's build target)", ev)
    if system == "Windows":
        return Check("platform", WARNING, "Windows host: the kernel runs inside WSL; run this preflight "
                     "inside WSL to check the kernel itself", ev)
    return Check("platform", BLOCKED, f"{system} {machine}: the D-Flow FM 1.2.184 build is Linux x86_64 "
                 "only (docs/dflowfm_kernel_build.md); it cannot run natively here", ev)


def check_kernel(kernel: str | Path | None = None) -> Check:
    path, origin = _kernel(kernel)
    ev = {"path": str(path), "origin": origin}
    if not path.is_file():
        return Check("kernel", BLOCKED, f"kernel not found at {path} ({origin})", ev)
    if not os.access(path, os.X_OK):
        return Check("kernel", BLOCKED, f"kernel at {path} is not executable", ev)
    dflowfm = path.parent / "dflowfm"
    ev["dflowfm_binary"] = str(dflowfm) if dflowfm.is_file() else None
    return Check("kernel", PASS, f"executable kernel at {path} ({origin})", ev)


def check_time_binary(path: Path = TIME_BINARY) -> Check:
    if path.is_file() and os.access(path, os.X_OK):
        return Check("usr_bin_time", PASS, f"{path} present", {"path": str(path)})
    return Check("usr_bin_time", BLOCKED, f"{path} missing: the launcher cannot write "
                 "output/resource_usage.txt, which the real-run check requires", {"path": str(path)})


def check_python_modules() -> list[Check]:
    from importlib import metadata

    out = []
    for module, pinned in PYTHON_MODULES:
        try:
            importlib.import_module(module)
        except Exception as e:  # ImportError or a broken native extension
            out.append(Check(f"python:{module}", BLOCKED, f"cannot import {module}: {e}"))
            continue
        version = None
        dist = VERSION_DISTRIBUTIONS.get(module)
        if dist:
            try:
                version = metadata.version(dist)
            except metadata.PackageNotFoundError:
                version = None
        if pinned and version != pinned:
            out.append(Check(f"python:{module}", WARNING, f"{module} {version} imported; pinned {pinned}",
                             {"version": version, "pinned": pinned}))
        else:
            out.append(Check(f"python:{module}", PASS, f"{module} {version or ''}".strip(),
                             {"version": version}))
    return out


def check_disk(data_dir: Path, min_free_gb: float | None) -> Check:
    probe = data_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    ev = {"path": str(probe), "free_gb": round(free_gb, 2), "min_free_gb": min_free_gb}
    if min_free_gb is None:
        return Check("disk", WARNING, f"{free_gb:.1f} GB free at {probe}; no --min-free-gb given, so "
                     "nothing was compared (see docs/run_budget.md for per-run sizes)", ev)
    if free_gb < min_free_gb:
        return Check("disk", BLOCKED, f"{free_gb:.1f} GB free at {probe} < {min_free_gb} GB required", ev)
    return Check("disk", PASS, f"{free_gb:.1f} GB free at {probe} (>= {min_free_gb} GB)", ev)


def _load_config(site_id: str, data_dir: Path, sites_dir: Path | None):
    if sites_dir is None and onboarded_config_path(site_id, data_dir).is_file():
        sites_dir = onboarded_config_path(site_id, data_dir).parent
    # The placeholder banner is reported as a count in this check instead of printed.
    log = logging.getLogger("backend.shared.site_config")
    previous = log.level
    log.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return load_site_config(site_id, sites_dir=sites_dir), sites_dir
    finally:
        log.setLevel(previous)


def check_site_config(site_id: str, data_dir: Path, sites_dir: Path | None):
    try:
        cfg, used = _load_config(site_id, data_dir, sites_dir)
    except Exception as e:
        return Check("site_config", BLOCKED, f"cannot load site config '{site_id}': {e}"), None
    where = str(used) if used else "repo sites/ (or the onboarded config)"
    return Check("site_config", PASS, f"loaded '{site_id}' from {where}",
                 {"sites_dir": str(used) if used else None,
                  "placeholder_fields": len(cfg.placeholder_fields)}), cfg


def check_base_flow(cfg) -> Check:
    bf = cfg.domains.far_field.inflow.base_flow
    if bf is None or bf.value is None:
        ev = {} if bf is None else {"status": bf.status, "source": bf.source}
        return Check("base_flow", BLOCKED, "domains.far_field.inflow.base_flow is null; a sourced value or "
                     "an explicit team decision is required -- no default is supplied", ev)
    ev = {"value": bf.value, "unit": bf.unit, "status": bf.status, "source": bf.source}
    if bf.status != "sourced":
        return Check("base_flow", WARNING, f"base_flow {bf.value} {bf.unit} is a placeholder "
                     f"(source: {bf.source}); results will carry has_placeholders", ev)
    return Check("base_flow", PASS, f"base_flow {bf.value} {bf.unit} (source: {bf.source})", ev)


def check_terrain(site_id: str, data_dir: Path) -> Check:
    terrain = data_dir / site_id / "terrain"
    missing = [name for name in TERRAIN_FILES if not (terrain / name).is_file()]
    ev = {"dir": str(terrain), "missing": missing}
    if missing:
        return Check("terrain", BLOCKED, f"missing terrain inputs in {terrain}: {', '.join(missing)}", ev)
    return Check("terrain", PASS, f"all {len(TERRAIN_FILES)} terrain inputs present in {terrain}", ev)


def check_breach(site_id: str, data_dir: Path, cfg) -> Check:
    path = data_dir / site_id / "breach" / "breach_params.json"
    if not path.is_file():
        return Check("breach", BLOCKED, f"{path} missing -- run the breach stage (Feature 3)")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return Check("breach", BLOCKED, f"{path} is not valid JSON: {e}")
    dam_id = cfg.domains.far_field.inflow.from_ if cfg is not None else None
    records = [r for r in (payload.get("provenance") or {}).get("reference_hydrographs", [])
               if dam_id is None or r.get("dam_id") == dam_id]
    written = [r for r in records if r.get("status") == "written"]
    ev = {"path": str(path), "inflow_dam": dam_id,
          "reference_hydrographs": {r.get("case"): r.get("status") for r in records},
          "blocked_reasons": sorted({r.get("reason") for r in records if r.get("status") == "blocked"})}
    if not written:
        return Check("breach", BLOCKED, f"no reference hydrograph could be written for inflow dam "
                     f"'{dam_id}': {'; '.join(ev['blocked_reasons']) or 'no records'}", ev)
    return Check("breach", PASS, f"{len(written)} reference hydrograph(s) written for '{dam_id}'", ev)


def check_design(site_id: str, data_dir: Path) -> Check:
    path = data_dir / site_id / "design" / "scenario_design.json"
    if not path.is_file():
        return Check("scenario_design", WARNING, f"{path} missing; the campaign would build it from "
                     "breach_params.json (Feature 4)", {"path": str(path)})
    try:
        design = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return Check("scenario_design", BLOCKED, f"{path} is not valid JSON: {e}")
    ev = {"path": str(path), "n_scenarios": len(design.get("scenarios", [])),
          "n_extra": len(design.get("extra", []))}
    if design.get("site_id") != site_id or design.get("model") != "delft3d":
        return Check("scenario_design", BLOCKED, f"design is for site {design.get('site_id')!r} / model "
                     f"{design.get('model')!r}, not {site_id!r} / 'delft3d'", ev)
    recorded = (design.get("provenance") or {}).get("breach_params_sha256")
    bp = data_dir / site_id / "breach" / "breach_params.json"
    if recorded and bp.is_file():
        current = hashlib.sha256(bp.read_bytes()).hexdigest()
        ev["breach_params_sha256_matches"] = current == recorded
        if current != recorded:
            return Check("scenario_design", BLOCKED, "design was built from a different breach_params.json "
                         "(sha256 mismatch) -- rebuild the design", ev)
    if not ev["n_scenarios"]:
        return Check("scenario_design", BLOCKED, "design has no scenarios", ev)
    return Check("scenario_design", PASS, f"{ev['n_scenarios']} design scenario(s), {ev['n_extra']} extra", ev)


def run_preflight(site_id: str, *, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
                  kernel: str | Path | None = None, min_free_gb: float | None = None) -> dict:
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    sites_dir = Path(sites_dir) if sites_dir is not None else None
    checks = [check_platform(), check_kernel(kernel), check_time_binary(), *check_python_modules(),
              check_disk(data_dir, min_free_gb)]
    site_check, cfg = check_site_config(site_id, data_dir, sites_dir)
    checks.append(site_check)
    if cfg is not None:
        checks.append(check_base_flow(cfg))
    checks += [check_terrain(site_id, data_dir), check_breach(site_id, data_dir, cfg),
               check_design(site_id, data_dir)]
    statuses = {c.status for c in checks}
    overall = BLOCKED if BLOCKED in statuses else WARNING if WARNING in statuses else PASS
    return {"site_id": site_id, "data_dir": str(data_dir), "overall": overall,
            "checks": [asdict(c) for c in checks]}


def format_report(report: dict) -> str:
    lines = [f"D-Flow FM preflight -- site {report['site_id']} -- data dir {report['data_dir']}"]
    for c in report["checks"]:
        lines.append(f"  [{c['status']:<7}] {c['name']}: {c['detail']}")
    lines.append(f"OVERALL: {report['overall']}")
    return "\n".join(lines)

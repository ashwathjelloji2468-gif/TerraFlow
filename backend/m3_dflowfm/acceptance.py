"""Feature 13, phase 2: acceptance harness for a finished D-Flow FM run.

A run that finished (M3 rule 1) is not automatically a usable flood result. This module evaluates
every acceptance check SEPARATELY and records the evidence it used. Each check is one of:

- ``PASS``         -- the evidence exists and satisfies the check.
- ``FAIL``         -- the evidence exists and does not satisfy the check.
- ``UNAVAILABLE``  -- the evidence needed for the check is missing. Never treated as PASS.

The overall status is ``ACCEPTED`` only when every REQUIRED check is ``PASS``; otherwise it is
``FAILED_ACCEPTANCE`` (with the failing / unavailable checks listed). Optional checks (mass/source
balance: no approved tolerance exists in config or docs) are reported but cannot accept or reject.

Reuses `launcher.check_success`, `smoke.wet_at_pois`, `smoke.outlet_wet`, `smoke.downstream_wet_faces`
and `hydraulic_path.source_outlet_bottleneck` -- no solver physics is changed and nothing is simulated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
ACCEPTED, FAILED_ACCEPTANCE = "ACCEPTED", "FAILED_ACCEPTANCE"

REQUIRED_CHECKS = ("solver_success", "postprocess_success", "required_outputs", "run_identity",
                   "terrain_compatibility", "forcing_provenance", "hydraulic_path", "poi_wetting")
OPTIONAL_CHECKS = ("mass_source",)
REQUIRED_RUN_OUTPUTS = ("run_meta.json", "summary/max_depth.tif", "summary/max_velocity.tif",
                        "summary/arrival_time.tif", "timeseries.csv")


def _check(status: str, detail: str, **evidence) -> dict:
    return {"status": status, "detail": detail, "evidence": evidence}


def _run_meta(run_dir: Path) -> dict | None:
    path = run_dir / "run_meta.json"
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def check_solver(case_dir: Path | None, model_stem: str | None) -> dict:
    from . import launcher
    if case_dir is None or model_stem is None or not Path(case_dir).is_dir():
        return _check(UNAVAILABLE, "case directory / model stem not available")
    result = launcher.check_success(case_dir, model_stem)
    return _check(PASS if result["success"] else FAIL,
                  "M3 rule 1: no ** ERROR in .dia and _map.nc/_his.nc exist (exit code not consulted)", **result)


def check_postprocess(meta: dict | None) -> dict:
    if meta is None:
        return _check(UNAVAILABLE, "run_meta.json missing or unreadable")
    from backend.m0_api import schemas
    try:
        schemas.validate("run_meta.schema.json", meta)
    except Exception as e:  # noqa: BLE001 - any validation failure is a FAIL with its message
        return _check(FAIL, f"run_meta.json does not validate: {str(e)[:200]}")
    if meta.get("status") != "postprocessed":
        return _check(FAIL, f"run_meta status is {meta.get('status')!r}, not 'postprocessed'")
    return _check(PASS, "run_meta.json schema-valid with status 'postprocessed'")


def check_outputs(run_dir: Path) -> dict:
    missing = [rel for rel in REQUIRED_RUN_OUTPUTS if not (run_dir / rel).is_file() or (run_dir / rel).stat().st_size == 0]
    return _check(FAIL if missing else PASS, "required post-processed outputs present and non-empty",
                  required=list(REQUIRED_RUN_OUTPUTS), missing=missing)


def check_identity(meta: dict | None, run_id: str, scenario_id: str, model: str) -> dict:
    if meta is None:
        return _check(UNAVAILABLE, "run_meta.json missing or unreadable")
    got = {k: meta.get(k) for k in ("run_id", "scenario_id", "model")}
    want = {"run_id": run_id, "scenario_id": scenario_id, "model": model}
    return _check(PASS if got == want else FAIL, "run_meta identity matches the registered run", got=got, expected=want)


def check_terrain(run_dir: Path, terrain_dir: Path | None) -> dict:
    """The run's summary rasters sit exactly on the terrain's canonical grid (domain_mask.tif)."""
    import rasterio
    depth, mask = run_dir / "summary" / "max_depth.tif", (Path(terrain_dir) / "domain_mask.tif") if terrain_dir else None
    if mask is None or not mask.is_file() or not depth.is_file():
        return _check(UNAVAILABLE, "terrain domain_mask.tif or summary/max_depth.tif missing",
                      terrain_dir=str(terrain_dir) if terrain_dir else None)
    try:
        with rasterio.open(depth) as a, rasterio.open(mask) as b:
            same = a.shape == b.shape and a.crs == b.crs and a.transform.almost_equals(b.transform)
            ev = {"run_shape": list(a.shape), "terrain_shape": list(b.shape),
                  "run_crs": str(a.crs), "terrain_crs": str(b.crs)}
    except Exception as e:  # noqa: BLE001
        return _check(UNAVAILABLE, f"could not read rasters: {type(e).__name__}")
    return _check(PASS if same else FAIL, "summary grid identical to the terrain canonical grid", **ev)


def check_forcing(meta: dict | None, site_dir: Path) -> dict:
    if meta is None:
        return _check(UNAVAILABLE, "run_meta.json missing or unreadable")
    refs = list(meta.get("hydrographs") or [])
    if meta.get("forcing_provenance_path"):
        refs.append(meta["forcing_provenance_path"])
    if not refs:
        return _check(UNAVAILABLE, "run_meta records no hydrograph / forcing provenance file")
    missing = [r for r in refs if not ((site_dir / r).is_file() or Path(r).is_file())]
    return _check(FAIL if missing else PASS, "forcing provenance files recorded and present",
                  references=refs, missing=missing, input_forcing_status=meta.get("input_forcing_status"))


def check_hydraulic_path(case_dir: Path | None, model_stem: str | None) -> dict:
    """From the solver's own map output: the configured outlet received water (and, diagnostically,
    how much ponding the mesh needs to connect source -> outlet, when the net is available)."""
    from . import smoke
    if case_dir is None or model_stem is None:
        return _check(UNAVAILABLE, "case directory / model stem not available")
    case_dir = Path(case_dir)
    map_nc, pli = case_dir / "output" / f"{model_stem}_map.nc", case_dir / "inputs" / "downstream_outlet.pli"
    if not map_nc.is_file() or not pli.is_file():
        return _check(UNAVAILABLE, "map output or downstream_outlet.pli missing", map=str(map_nc), outlet_pli=str(pli))
    try:
        outlet = smoke.outlet_wet(map_nc, pli)
    except Exception as e:  # noqa: BLE001
        return _check(UNAVAILABLE, f"could not read map output: {type(e).__name__}")
    ev: dict = {"outlet": outlet}
    source, out_xy = smoke._source_xy(case_dir), smoke._outlet_xy(case_dir)
    if source and out_xy:
        try:
            ev["downstream"] = smoke.downstream_wet_faces(map_nc, source, out_xy)
        except Exception:  # noqa: BLE001 - diagnostic only
            pass
        net = case_dir / "inputs" / "domain_net.nc"
        if net.is_file():
            try:
                from .hydraulic_path import source_outlet_bottleneck
                ev["mesh_bottleneck"] = source_outlet_bottleneck(net, source, pli)
            except Exception:  # noqa: BLE001 - diagnostic only
                pass
    return _check(PASS if outlet["wet"] else FAIL, "outlet faces wet in the solver's map output (source connected downstream)", **ev)


def check_poi_wetting(case_dir: Path | None, model_stem: str | None, spinup_s: float, threshold_m: float) -> dict:
    """The POI smoke gate, unchanged: EVERY configured history station must be wet after spin-up."""
    from . import smoke
    if case_dir is None or model_stem is None:
        return _check(UNAVAILABLE, "case directory / model stem not available")
    his = Path(case_dir) / "output" / f"{model_stem}_his.nc"
    if not his.is_file():
        return _check(UNAVAILABLE, "history output missing", his=str(his))
    try:
        result = smoke.wet_at_pois(his, spinup_s, threshold_m)
    except Exception as e:  # noqa: BLE001
        return _check(UNAVAILABLE, f"could not read history output: {type(e).__name__}")
    if not result["per_poi"]:
        return _check(UNAVAILABLE, "history output has no POI stations", **result)
    return _check(PASS if result["all_wet"] else FAIL, "every POI wet after spin-up (smoke gate)", **result)


def check_mass_source(meta: dict | None) -> dict:
    value = (meta or {}).get("mass_balance_error_pct")
    if value is None:
        return _check(UNAVAILABLE, "no mass-balance output recorded for this run")
    return _check(UNAVAILABLE, "mass-balance error reported, but no approved tolerance exists in config/docs; "
                  "recorded for review, not judged", mass_balance_error_pct=value)


def evaluate_run(run_dir: str | Path, *, run_id: str, scenario_id: str, site_dir: str | Path,
                 case_dir: str | Path | None = None, model_stem: str | None = None,
                 terrain_dir: str | Path | None = None, model: str = "delft3d",
                 spinup_s: float = 0.0, threshold_m: float | None = None) -> dict:
    """Evaluate every acceptance check for one run; returns the `acceptance` block (module docstring)."""
    from backend.m3_common.postprocess import PostprocessConfig
    from backend.shared.version import version_info
    run_dir, site_dir = Path(run_dir), Path(site_dir)
    case_dir = Path(case_dir) if case_dir else None
    threshold_m = PostprocessConfig().arrival_m if threshold_m is None else threshold_m
    meta = _run_meta(run_dir)
    checks = {
        "solver_success": check_solver(case_dir, model_stem),
        "postprocess_success": check_postprocess(meta),
        "required_outputs": check_outputs(run_dir),
        "run_identity": check_identity(meta, run_id, scenario_id, model),
        "terrain_compatibility": check_terrain(run_dir, Path(terrain_dir) if terrain_dir else None),
        "forcing_provenance": check_forcing(meta, site_dir),
        "hydraulic_path": check_hydraulic_path(case_dir, model_stem),
        "poi_wetting": check_poi_wetting(case_dir, model_stem, spinup_s, threshold_m),
        "mass_source": check_mass_source(meta),
    }
    not_passed = [k for k in REQUIRED_CHECKS if checks[k]["status"] != PASS]
    return {
        "status": ACCEPTED if not not_passed else FAILED_ACCEPTANCE,
        "failed_checks": [k for k in not_passed if checks[k]["status"] == FAIL],
        "unavailable_checks": [k for k in not_passed if checks[k]["status"] == UNAVAILABLE],
        "required_checks": list(REQUIRED_CHECKS), "optional_checks": list(OPTIONAL_CHECKS),
        "checks": checks, "spinup_s": spinup_s, "poi_threshold_m": threshold_m,
        "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **version_info(),
    }

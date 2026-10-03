"""Feature 13, phase 4: reproducible registration of an existing D-Flow FM run + the site's
scenario-library index.

`register_existing_run` replaces hand-made registry rows (the Teesta MVP run was once registered by
hand on one machine, so every other checkout answered `real_run_not_found`). It imports a run
directory that already exists on disk -- it never writes solver outputs or invents run metadata:

- `run_meta.json` missing / unreadable / identity mismatch  -> refused
- required post-processed outputs missing                     -> refused
- acceptance FAILED_ACCEPTANCE                                -> refused (unless `pilot=True`)
- `pilot=True`: integrity checks (solver, postprocess, outputs, identity) must PASS; the run is
  registered as a PILOT run with its acceptance result recorded as-is and the
  `pilot_run_not_production_accepted` caveat. It is never added to the M5 training cache.

Only an ACCEPTED (non-pilot) run becomes a normal queryable run and enters the M5 cache.

CLI:
  python -m backend.m0_api.run_registration register <site_id> <run_id> [--pilot] [--case-dir DIR]
                                                     [--model-stem STEM] [--spinup-s S] [--data-dir DIR]
  python -m backend.m0_api.run_registration index <site_id> [--data-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.m0_api import registry, run_metadata

PILOT_CAVEAT = {"id": "pilot_run_not_production_accepted", "severity": "warning",
                "text_key": "caveat_pilot_run_not_production_accepted"}
# Feature 17: a pilot run must also have actually finished (simulation_completed, decision E4).
INTEGRITY_CHECKS = ("solver_success", "simulation_completed", "postprocess_success", "required_outputs", "run_identity")


class RegistrationRefused(ValueError):
    """The run cannot be registered; the message says which check refused it."""


def acceptance_status(meta: dict) -> str | None:
    return (meta.get("acceptance") or {}).get("status")


def is_queryable(meta: dict) -> bool:
    """A delft3d run is queryable only when ACCEPTED, or explicitly registered as a PILOT run."""
    return acceptance_status(meta) == "ACCEPTED" or meta.get("run_class") == "pilot"


def _model_stem(case_dir: Path) -> str | None:
    mdus = sorted(case_dir.glob("*.mdu")) if case_dir.is_dir() else []
    return mdus[0].stem if len(mdus) == 1 else None


def register_existing_run(site_id: str, run_id: str, *, data_dir: str | Path | None = None,
                          case_dir: str | Path | None = None, model_stem: str | None = None,
                          pilot: bool = False, spinup_s: float = 0.0, model: str = "delft3d") -> dict:
    from backend.m3_dflowfm.acceptance import ACCEPTED, PASS, evaluate_run
    data_dir = Path(data_dir) if data_dir is not None else registry.data_dir()
    site_dir = data_dir / site_id
    run_dir = site_dir / "runs" / run_id
    meta_path = run_dir / "run_meta.json"
    if not meta_path.is_file() or meta_path.stat().st_size == 0:
        raise RegistrationRefused(f"run_meta.json missing for {site_id}/{run_id}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RegistrationRefused(f"run_meta.json unreadable for {site_id}/{run_id}: {e}") from e
    scenario_id = meta.get("scenario_id")
    if meta.get("run_id") != run_id or not scenario_id or meta.get("model") != model \
            or run_id != f"{scenario_id}__{model}":
        raise RegistrationRefused(f"run_meta.json identity does not match {run_id} "
                                  f"(run_id={meta.get('run_id')!r}, scenario_id={scenario_id!r}, model={meta.get('model')!r})")
    case = Path(case_dir) if case_dir else Path(meta.get("case_dir") or (meta.get("artifact_paths") or {}).get("case")
                                                 or run_dir / "case")
    stem = model_stem or meta.get("model_stem") or _model_stem(case)
    acceptance = evaluate_run(run_dir, run_id=run_id, scenario_id=scenario_id, site_dir=site_dir, case_dir=case,
                              model_stem=stem, terrain_dir=run_metadata.terrain_dir(site_dir, run_id, meta),
                              model=model, spinup_s=spinup_s)
    checks = acceptance["checks"]
    if checks["required_outputs"]["status"] != PASS:
        raise RegistrationRefused("required outputs missing: " + ", ".join(checks["required_outputs"]["evidence"]["missing"]))
    if pilot:
        failed = [c for c in INTEGRITY_CHECKS if checks[c]["status"] != PASS]
        if failed:
            raise RegistrationRefused("pilot registration needs passing integrity checks; not passed: " + ", ".join(failed))
    elif acceptance["status"] != ACCEPTED:
        raise RegistrationRefused("FAILED_ACCEPTANCE: failed " + (", ".join(acceptance["failed_checks"]) or "none")
                                  + "; unavailable " + (", ".join(acceptance["unavailable_checks"]) or "none"))

    original = run_dir / "run_meta.original.json"
    if not original.exists():  # keep the solver-pipeline metadata exactly as it was written
        original.write_text(meta_path.read_text(encoding="utf-8"), encoding="utf-8")
    meta["acceptance"] = acceptance
    meta["run_class"] = "pilot" if pilot else "accepted"
    if pilot:
        caveats = list(meta.get("caveats") or [])
        if PILOT_CAVEAT["id"] not in caveats:
            caveats.append(PILOT_CAVEAT["id"])
        meta["caveats"] = caveats
    meta["registration"] = {"registered_at": registry.utc_now(), "method": "run_registration.register_existing_run",
                            "case_dir": str(case), "model_stem": stem}
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (run_dir / "acceptance.json").write_text(json.dumps(acceptance, indent=2) + "\n", encoding="utf-8")

    registry.init_db()
    conn = registry.connect()
    try:
        with conn:
            params = {}
            scen_file = run_dir / "scenario.json"
            if scen_file.is_file():
                params = json.loads(scen_file.read_text(encoding="utf-8")).get("params", {})
            conn.execute("INSERT OR IGNORE INTO scenarios (scenario_id, site_id, kind, params_json, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (scenario_id, site_id, "named" if pilot else "design",
                                                   json.dumps(params), registry.utc_now()))
            conn.execute("INSERT OR REPLACE INTO runs (run_id, scenario_id, model, status, run_dir, meta_json, finished_at) "
                         "VALUES (?, ?, ?, 'postprocessed', ?, ?, ?)",
                         (run_id, scenario_id, model, str(run_dir), json.dumps({"case_dir": str(case), "model_stem": stem,
                                                                               "run_class": meta["run_class"],
                                                                               "acceptance_status": acceptance["status"]}),
                          meta.get("finished_at")))
    finally:
        conn.close()
    if not pilot and params:
        from backend.m5_emulator.run_cache import register_run
        register_run(data_dir, site_id, run_id, params, meta)
    write_run_index(data_dir, site_id)
    return {"run_id": run_id, "scenario_id": scenario_id, "run_class": meta["run_class"],
            "acceptance_status": acceptance["status"], "queryable": True}


def write_run_index(data_dir: str | Path, site_id: str) -> Path:
    """`data/<site>/runs/index.json`: every registered run of the site with its registry status,
    campaign state, acceptance status, run class and whether /flood/query may serve it."""
    from backend.campaign import run_state
    data_dir = Path(data_dir)
    conn = registry.connect()
    try:
        rows = conn.execute("SELECT r.* FROM runs r JOIN scenarios s ON s.scenario_id = r.scenario_id "
                            "WHERE s.site_id = ? ORDER BY r.run_id", (site_id,)).fetchall()
    finally:
        conn.close()
    entries = []
    for r in rows:
        meta_path = Path(r["run_dir"]) / "run_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except ValueError:
            meta = {}
        merged = {**json.loads(r["meta_json"] or "{}"), **meta}
        prov = merged.get("provenance") or {}
        entries.append({
            "run_id": r["run_id"], "scenario_id": r["scenario_id"], "model": r["model"],
            "registry_status": r["status"], "state": run_state(r["status"], merged),
            "acceptance_status": acceptance_status(merged) or merged.get("acceptance_status"),
            "run_class": merged.get("run_class"),
            "queryable": r["status"] in ("completed", "postprocessed") and (r["model"] != "delft3d" or is_queryable(merged)),
            "input_fingerprint": prov.get("input_fingerprint"), "code_version": prov.get("code_version"),
            "completed_at": prov.get("completed_at") or r["finished_at"],
            "has_placeholders": merged.get("has_placeholders"),
        })
    target = data_dir / site_id / "runs" / "index.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"site_id": site_id, "updated_at": registry.utc_now(), "runs": entries}, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(target)
    return target


READINESS_RUN_STATES = ("ACCEPTED", "PILOT", "FAILED_ACCEPTANCE", "FAILED", "QUEUED", "RUNNING", "NOT_EVALUATED")


def readiness_run_status(registry_status: str, meta: dict, model: str) -> str:
    acc = acceptance_status(meta)
    if registry_status == "failed":
        return "FAILED_ACCEPTANCE" if acc == "FAILED_ACCEPTANCE" else "FAILED"
    if registry_status == "queued":
        return "QUEUED"
    if registry_status == "running":
        return "RUNNING"
    if meta.get("run_class") == "pilot":
        return "PILOT"
    if acc == "ACCEPTED":
        return "ACCEPTED"
    return "FAILED_ACCEPTANCE" if acc == "FAILED_ACCEPTANCE" else "NOT_EVALUATED"


def site_readiness(site_id: str, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
                   checklist_path: str | Path | None = None) -> dict:
    """`GET /sites/{id}/readiness` payload: the production gate per input + every registered run's
    acceptance state. Missing data is never success: an absent run list is an empty list with zero
    counts, and the verdict comes only from the gate."""
    from backend.m3_dflowfm import production_gate
    data_dir = Path(data_dir) if data_dir is not None else registry.data_dir()
    kwargs = {"checklist_path": checklist_path} if checklist_path else {}
    gate = production_gate.evaluate(site_id, data_dir=data_dir, sites_dir=sites_dir, **kwargs)
    conn = registry.connect()
    try:
        rows = conn.execute("SELECT r.* FROM runs r JOIN scenarios s ON s.scenario_id = r.scenario_id "
                            "WHERE s.site_id = ? ORDER BY r.run_id", (site_id,)).fetchall()
    finally:
        conn.close()
    runs = []
    for r in rows:
        meta_path = Path(r["run_dir"]) / "run_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except ValueError:
            meta = {}
        merged = {**json.loads(r["meta_json"] or "{}"), **meta}
        acc = merged.get("acceptance") or {}
        status = readiness_run_status(r["status"], merged, r["model"])
        runs.append({"run_id": r["run_id"], "scenario_id": r["scenario_id"], "model": r["model"], "status": status,
                     "registry_status": r["status"], "acceptance_status": acc.get("status"),
                     "run_class": merged.get("run_class"),
                     "queryable": r["status"] in ("completed", "postprocessed") and (r["model"] != "delft3d" or is_queryable(merged)),
                     "failed_checks": list(acc.get("failed_checks") or []),
                     "unavailable_checks": list(acc.get("unavailable_checks") or [])})
    caveats = []
    if gate["verdict"] != production_gate.READY:
        caveats.append({"id": "production_gate_blocked", "severity": "warning", "text_key": "caveat_production_gate_blocked"})
    if any(r["status"] == "PILOT" for r in runs):
        caveats.append(dict(PILOT_CAVEAT))
    return {"site_id": site_id, "model": "delft3d", "evaluated_at": gate["evaluated_at"],
            "production_verdict": gate["verdict"], "config_loaded": gate["config_loaded"],
            "inputs": [{k: i[k] for k in ("key", "category", "status", "blocking", "source", "detail")} for i in gate["items"]],
            "blocking": gate["blocking"],
            "counts": gate.get("counts") or {s: sum(1 for i in gate["items"] if i["status"] == s)
                                             for s in ("READY", "PLACEHOLDER", "BLOCKED", "CONTROLLED_PASS")},
            "runs": runs, "run_counts": {s: sum(1 for r in runs if r["status"] == s) for s in READINESS_RUN_STATES},
            "caveats": caveats}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register an existing D-Flow FM run / write the run index")
    sub = parser.add_subparsers(dest="cmd", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("site_id")
    reg.add_argument("run_id")
    reg.add_argument("--pilot", action="store_true", help="register as a PILOT run (never production-accepted)")
    reg.add_argument("--case-dir", type=Path)
    reg.add_argument("--model-stem")
    reg.add_argument("--spinup-s", type=float, default=0.0)
    reg.add_argument("--data-dir", type=Path)
    idx = sub.add_parser("index")
    idx.add_argument("site_id")
    idx.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    if args.data_dir is not None:
        import os
        os.environ["SIH26_DATA_DIR"] = str(args.data_dir.resolve())
    if args.cmd == "index":
        print(write_run_index(registry.data_dir(), args.site_id))
        return 0
    try:
        out = register_existing_run(args.site_id, args.run_id, case_dir=args.case_dir, model_stem=args.model_stem,
                                    pilot=args.pilot, spinup_s=args.spinup_s)
    except RegistrationRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

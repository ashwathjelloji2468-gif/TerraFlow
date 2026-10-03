"""Run M3/M4 scenario sets through the M0 job system.

D-Flow FM scenarios are generated from the M5 design and persisted before
launch, allowing the worker to resume after process or host restarts. SPH
retains its existing case-generation path.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.m0_api import jobs, registry
from backend.shared.site_config import load_site_config

CONTRACT_VERSION = "0.3.0"


@dataclass(frozen=True)
class CampaignCaseResult:
    scenario_id: str
    run_id: str
    status: str  # queued | completed | postprocessed | failed | refused | blocked | preserved
    reason: str | None = None


def _scenario_params(design: dict, scenario_id: str) -> dict | None:
    for entry in design["scenarios"] + design["extra"]:
        if entry["scenario_id"] == scenario_id:
            return entry["params"]
    return None


def _load_design(data_dir: Path, site_id: str) -> dict | None:
    path = data_dir / site_id / "design" / "scenario_design.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def write_dflowfm_status(data_dir: Path, site_id: str, conn: sqlite3.Connection, job_id: str) -> None:
    """Atomically publish campaign counts and ETA from completed runs."""
    row = jobs.get_job(conn, job_id)
    if row is None:
        return
    runs = jobs.job_runs(conn, row)
    dry_run = bool(jobs.payload(row).get("dry_run"))
    done = [r for r in runs if r["status"] == "postprocessed" or (dry_run and r["status"] == "completed")]
    active = next((r for r in runs if r["status"] == "running"), None)
    if active is None and not dry_run:
        active = next((r for r in runs if r["status"] == "queued"), None)
    failed = [r for r in runs if r["status"] == "failed"]
    states = {r["run_id"]: run_state(r["status"], json.loads(r["meta_json"] or "{}"), dry_run) for r in runs}
    durations = [json.loads(r["meta_json"] or "{}").get("wall_time_s") for r in done]
    durations = [float(v) for v in durations if v is not None]
    eta = (0 if dry_run else
           round(sum(durations) / len(durations) * (len(runs) - len(done))) if durations else None)
    body = {"job_id": job_id, "model": "delft3d", "updated_at": registry.utc_now(),
            "done": len(done), "total": len(runs), "running": active["run_id"] if active else None,
            "failed": [r["run_id"] for r in failed], "eta_s": eta,
            "demo_mode": bool(jobs.payload(row).get("demo_mode")),
            "mode": jobs.payload(row).get("campaign_mode"),
            "summary": {s: sum(1 for v in states.values() if v == s) for s in CAMPAIGN_STATES},
            "failed_acceptance": [rid for rid, v in states.items() if v == "FAILED_ACCEPTANCE"],
              "runs": [{"run_id": r["run_id"], "scenario_id": r["scenario_id"],
                      "status": ("done" if r["status"] == "postprocessed" or (dry_run and r["status"] == "completed") else
                                "failed" if r["status"] == "failed" else
                                "running" if r["status"] in {"queued", "running", "completed"} else r["status"]),
                      "state": states[r["run_id"]], "error": r["error"]} for r in runs]}
    target = data_dir / site_id / "campaign_status.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def run_dflowfm_campaign(site_id: str, conn: sqlite3.Connection, data_dir: str | Path | None = None,
                         sites_dir: str | Path | None = None, *, extra: list[str] | None = None,
                         dry_run: bool = False, demo: bool = False,
                         job_id: str | None = None, production: bool = False,
                         checklist_path: str | Path | None = None) -> tuple[str | None, list[CampaignCaseResult]]:
    """Build and register M5 design scenarios and explicitly selected extras.

    Feature 13: `production=True` refuses to build or queue anything unless the production-readiness
    gate (`m3_dflowfm.production_gate`) is READY -- the campaign is then BLOCKED, with the blocking
    inputs recorded in `campaign_status.json`. In every mode, a scenario whose run is already
    queued/running/postprocessed (or FAILED_ACCEPTANCE, kept for review) is preserved, never rebuilt
    or overwritten; each queued run carries its input fingerprint and code version."""
    from backend.m3_dflowfm import generator
    from backend.m5_emulator import scenario_design
    from dataclasses import replace

    data_dir = Path(data_dir) if data_dir is not None else registry.data_dir()
    if production:
        from backend.m3_dflowfm import production_gate
        gate = production_gate.evaluate(site_id, data_dir=data_dir, sites_dir=sites_dir,
                                        **({"checklist_path": checklist_path} if checklist_path else {}))
        if gate["verdict"] != production_gate.READY:
            _write_blocked_status(data_dir, site_id, gate)
            return None, [CampaignCaseResult(site_id, "", "blocked",
                                             "production gate BLOCKED: " + ", ".join(gate["blocking"]))]
    cfg = load_site_config(site_id, sites_dir=sites_dir)
    design = _load_design(data_dir, site_id)
    if design is not None and (design.get("site_id") != site_id or design.get("model") != "delft3d"):
        raise ValueError(f"M5 scenario design must be for site {site_id!r} and model 'delft3d'")
    design_was_missing = design is None
    if design is None:
        settings = scenario_design.load_scenario_design_settings()
        if demo:
            settings = replace(settings, n=4, n_holdout=0)
        design = scenario_design.build_scenario_design(cfg, cfg.domains.far_field.inflow.from_, settings,
                                                       data_dir=data_dir)
    if demo:
        # M5's emulator and LOOCV both require at least four training runs.
        design["scenarios"] = design["scenarios"][:4]
        design["n"] = len(design["scenarios"])
        for index, item in enumerate(design["scenarios"], 1):
            # The design schema uses kind=design; the registry carries kind=demo.
            item.update(scenario_id=f"{site_id}__demo_s{index:03d}", kind="design")
        design["extra"] = []
    from backend.m0_api import schemas
    schemas.validate("scenario_design.schema.json", design)
    if design_was_missing and not demo:
        path = data_dir / site_id / "design" / "scenario_design.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(design, indent=2) + "\n", encoding="utf-8")
    entries = list(design.get("scenarios", []))
    selected = set(extra or [])
    extra_entries = []
    for entry in design.get("extra", []):
        sid = entry.get("scenario_id", "")
        aliases = {sid, sid.rsplit("_", 1)[-1]}
        if "__n_" in sid:
            aliases.add(sid.split("__n_", 1)[1])
        if "_n_" in sid:
            aliases.add(sid.split("_n_", 1)[1])
        if "_hist_" in sid:
            aliases.add("hist_" + sid.split("_hist_", 1)[1])
            aliases.add(sid.split("_hist_", 1)[1])
        if aliases & selected:
            extra_entries.append(entry)
    entries += extra_entries
    matched = {alias for entry in extra_entries for alias in (entry["scenario_id"],
        entry["scenario_id"].rsplit("_", 1)[-1],
        entry["scenario_id"].split("__n_", 1)[-1] if "__n_" in entry["scenario_id"] else "",
        entry["scenario_id"].split("_n_", 1)[-1] if "_n_" in entry["scenario_id"] else "",
        entry["scenario_id"].split("_hist_", 1)[-1] if "_hist_" in entry["scenario_id"] else "")}
    unknown = selected - matched
    if unknown:
        raise ValueError(f"named extra scenario(s) not found in design: {', '.join(sorted(unknown))}")
    # Feature 13 isolation: never rebuild/overwrite an active, successful or under-review run. Those
    # scenarios are reported as `preserved` and stay with the job that created them.
    preserved: list[CampaignCaseResult] = []
    kept_entries = []
    for entry in entries:
        rid = f"{entry['scenario_id']}__delft3d"
        reason = _preserved_reason(conn, rid, dry_run)
        if reason:
            preserved.append(CampaignCaseResult(entry["scenario_id"], rid, "preserved", reason))
        else:
            kept_entries.append(entry)
    entries = kept_entries
    if not entries:
        return None, preserved
    campaign_payload = {"model": "delft3d", "dflowfm_campaign": True, "dry_run": dry_run,
                        "scenario_ids": [e["scenario_id"] for e in entries], "extra": sorted(selected),
                        "data_dir": str(data_dir.resolve()),
                        "sites_dir": str(Path(sites_dir).resolve()) if sites_dir else None}
    if job_id is None:
        job_id = jobs.create_job(conn, "campaign", site_id, demo_mode=demo, payload=campaign_payload)
    else:
        existing = jobs.get_job(conn, job_id)
        if existing is None or existing["site_id"] != site_id or existing["kind"] != "onboarding":
            raise ValueError("an attached campaign job must be an existing onboarding job for this site")
        jobs.update_payload(conn, job_id, **campaign_payload)
    run_ids, results = [], list(preserved)
    now = registry.utc_now()
    demo_stop_s = 9000.0
    if demo:
        # Keep all emulator training maps on the same run horizon. A breach
        # hydrograph can outlast the nominal short demo duration; extend every
        # scenario to the longest M2 hydrograph, rounded to M3's 30 s timestep.
        from backend.m2_breach.hydrograph import hydrograph
        from backend.m3_dflowfm.generator import SPINUP_S
        dam_id = cfg.domains.far_field.inflow.from_
        required_end = max(
            float(hydrograph(site_id, dam_id, entry["params"], sites_dir=sites_dir).t_s[-1]) + SPINUP_S
            for entry in entries
        )
        demo_stop_s = math.ceil(max(demo_stop_s, required_end) / 30.0) * 30.0
    from backend.shared.version import version_info
    config_sha = _sha256(cfg.model_dump_json())
    design_fp = (design.get("provenance") or {}).get("fingerprint")
    for entry in entries:
        sid, params = entry["scenario_id"], entry["params"]
        run_id = f"{sid}__delft3d"
        run_ids.append(run_id)
        run_dir = data_dir / site_id / "runs" / run_id
        case_dir = run_dir / "case"
        provenance = {"site_id": site_id, "scenario_id": sid, "run_id": run_id, "inputs": params,
                      "input_fingerprint": _sha256(json.dumps({"params": params, "site_config_sha256": config_sha,
                                                               "design_fingerprint": design_fp}, sort_keys=True)),
                      "site_config_sha256": config_sha, "design_fingerprint": design_fp,
                      "campaign_mode": "production" if production else ("demo" if demo else "non_production"),
                      "generated_at": registry.utc_now(), **version_info()}
        try:
            from backend.m2_breach.hydrograph import hydrograph, write_hydrograph
            dam_id = cfg.domains.far_field.inflow.from_
            hydro_path, _ = write_hydrograph(hydrograph(site_id, dam_id, params, sites_dir=sites_dir),
                                              site_id, sid, data_dir=data_dir)
            case_dir, meta = generator.build_case(site_id, sid, params, data_dir=data_dir,
                sites_dir=sites_dir, case_dir=case_dir,
                stop_s=demo_stop_s if demo else generator.DEFAULT_STOP_S, demo=demo)
            meta["hydrographs"] = [str(hydro_path.relative_to(data_dir / site_id))]
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "scenario.json").write_text(json.dumps({**entry, "provenance": provenance}, indent=2) + "\n",
                                                   encoding="utf-8")
            meta["provenance"] = provenance
            state, error = ("completed", None) if dry_run else ("queued", None)
            if dry_run:
                meta["dry_run"] = True
        except Exception as exc:
            state, error, meta = "failed", str(exc), {"params": params, "provenance": provenance}
        with conn:
            conn.execute("INSERT OR REPLACE INTO scenarios (scenario_id, site_id, kind, params_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (sid, site_id, "demo" if demo else entry.get("kind", "design"), json.dumps(params), now))
            conn.execute("INSERT OR REPLACE INTO runs (run_id, scenario_id, model, status, run_dir, meta_json, error) VALUES (?, ?, 'delft3d', ?, ?, ?, ?)",
                (run_id, sid, state, str(run_dir), json.dumps({**meta, "case_dir": str(case_dir), "attempt": 0}), error))
        results.append(CampaignCaseResult(sid, run_id, state, error))
    jobs.update_payload(conn, job_id, run_ids=run_ids, campaign_mode="production" if production else
                        ("demo" if demo else "non_production"))
    jobs.set_progress(conn, job_id, 0, len(run_ids), "runs")
    write_dflowfm_status(data_dir, site_id, conn, job_id)
    return job_id, results


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preserved_reason(conn: sqlite3.Connection, run_id: str, dry_run: bool) -> str | None:
    row = conn.execute("SELECT status, meta_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    status = row[0]
    meta = json.loads(row[1] or "{}")
    if status in ("queued", "running", "postprocessed") or (status == "completed" and not dry_run):
        return f"existing run is {status}; not rebuilt"
    if status == "failed" and (meta.get("acceptance") or {}).get("status") == "FAILED_ACCEPTANCE":
        return "existing run FAILED_ACCEPTANCE; outputs kept for review, not overwritten"
    return None


#: Feature 13 campaign states (summary of the registry status + acceptance result).
CAMPAIGN_STATES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "FAILED_ACCEPTANCE", "BLOCKED")


def run_state(status: str, meta: dict, dry_run: bool = False) -> str:
    acceptance = (meta.get("acceptance") or {}).get("status")
    if status == "failed":
        return "FAILED_ACCEPTANCE" if acceptance == "FAILED_ACCEPTANCE" else "FAILED"
    if status == "postprocessed" or (dry_run and status == "completed"):
        return "SUCCEEDED"
    if status in ("running", "completed"):
        return "RUNNING"
    return "QUEUED"


def _write_blocked_status(data_dir: Path, site_id: str, gate: dict) -> None:
    body = {"model": "delft3d", "updated_at": registry.utc_now(), "campaign_state": "BLOCKED",
            "mode": "production", "production_gate": {"verdict": gate["verdict"], "blocking": gate["blocking"]},
            "summary": {s: 0 for s in CAMPAIGN_STATES} | {"BLOCKED": 1}, "runs": []}
    target = data_dir / site_id / "campaign_status.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def _routed_discharge_from_delft3d(cfg, scenario_id: str, data_dir: Path, sph_run_id: str,
                                   section_width_m: float) -> Path:
    """Feature 5 -> M4: the SPH inlet discharge from the scenario's own postprocessed
    `<scenario_id>__delft3d` run, across a section normal to the centreline at the near-field
    inflow point. Raises FileNotFoundError/ValueError (-> refused) when that run is unavailable."""
    import geopandas as gpd
    from pyproj import Transformer

    from backend.m3_dflowfm.section_discharge import normal_section, write_section_discharge

    site_id = cfg.site.id
    m3_run_id = f"{scenario_id}__delft3d"
    m3_dir = data_dir / site_id / "runs" / m3_run_id
    meta_path = m3_dir / "run_meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"{m3_run_id} has no run_meta.json (not postprocessed)")
    if json.loads(meta_path.read_text()).get("status") != "postprocessed":
        raise ValueError(f"{m3_run_id} is not postprocessed")
    location = cfg.domains.near_field.inflow.location.value
    if location is None:
        raise ValueError("domains.near_field.inflow.location is null")
    epsg = int(cfg.crs.utm_epsg.value)
    x, y = Transformer.from_crs(4326, epsg, always_xy=True).transform(*location)
    centreline = gpd.read_file(data_dir / site_id / "terrain" / "centreline.gpkg").to_crs(epsg=epsg)
    line = max(centreline.geometry, key=lambda g: g.length)
    section = normal_section(line, x, y, section_width_m)
    _, sidecar = write_section_discharge(
        m3_dir, data_dir / site_id / "runs" / sph_run_id / "routed", section, site_id=site_id,
        scenario_id=scenario_id, source_run_id=m3_run_id, crs_epsg=epsg, section_id=f"{scenario_id}__nearfield_inflow")
    return sidecar


def run_sph_campaign(
    site_id: str,
    conn: sqlite3.Connection,
    data_dir: Path | None = None,
    sites_dir: str | Path | None = None,
) -> tuple[str | None, list[CampaignCaseResult]]:
    """Build and register every scenario in `site_id`'s `simulation.sph.scenarios`
    as a near-field SPH case, under one shared `campaign` job.

    Returns `(job_id, results)`. `job_id` is `None` if there is nothing to do
    (`simulation.sph.scenarios` is empty) -- no phantom job is created.
    A scenario missing from `design/scenario_design.json`, or whose case
    exceeds the VRAM budget (`generator.OverVramBudget`) or has no usable
    inflow yet (`generator.InflowUnavailable`), is reported as `"refused"`
    rather than silently dropped or guessed at.
    """
    from backend.m4_sph import generator  # local import: keep this module importable without geopandas

    data_dir = Path(data_dir) if data_dir is not None else registry.data_dir()
    cfg = load_site_config(site_id, sites_dir=sites_dir)
    scenario_ids = cfg.simulation.sph.scenarios
    if not scenario_ids:
        return None, []

    design = _load_design(data_dir, site_id)

    from backend.m4_sph.settings import load_sph_settings
    sph_settings = load_sph_settings()
    job_id = jobs.create_job(conn, "campaign", site_id,
        payload={"model": "sph", "sph_campaign": True, "scenario_ids": list(scenario_ids),
                 "data_dir": str(data_dir.resolve()),
                 "sites_dir": str(Path(sites_dir).resolve()) if sites_dir is not None else None,
                 "binaries_dir": sph_settings.binaries_dir})

    results: list[CampaignCaseResult] = []
    for scenario_id in scenario_ids:
        run_id = f"{scenario_id}__sph"
        params = _scenario_params(design, scenario_id) if design is not None else None
        if params is None:
            reason = (f"no 'design/scenario_design.json' for site '{site_id}'" if design is None
                      else f"scenario '{scenario_id}' not found in design/scenario_design.json")
            results.append(CampaignCaseResult(scenario_id, run_id, "refused", reason))
            jobs.log_event(conn, job_id, f"refused {run_id}: {reason}")
            continue

        routed_kwargs, routed_reason = {}, None
        if cfg.domains.near_field.inflow.from_ == "far_field":
            try:
                routed_kwargs["routed_discharge_path"] = _routed_discharge_from_delft3d(
                    cfg, scenario_id, data_dir, run_id, sph_settings.routed_section_width_m)
            except (FileNotFoundError, ValueError) as e:
                routed_reason = f"no routed Delft3D inflow: {e}"
        try:
            spec, case_meta = generator.build_nearfield_case(
                site_id, scenario_id, params, data_dir=data_dir, sites_dir=sites_dir, **routed_kwargs,
            )
        except (generator.OverVramBudget, generator.InflowUnavailable) as e:
            reason = f"{e} ({routed_reason})" if routed_reason else str(e)
            results.append(CampaignCaseResult(scenario_id, run_id, "refused", reason))
            jobs.log_event(conn, job_id, f"refused {run_id}: {reason}")
            continue

        run_directory = data_dir / site_id / "runs" / run_id
        terrain_dir = data_dir / site_id / "terrain"
        generator.write_case(spec, case_meta, run_directory, terrain_dir)

        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json)"
            " VALUES (?, ?, 'sph', 'queued', ?, ?)",
            (run_id, scenario_id, str(run_directory), json.dumps({**case_meta, "case_dir": str(run_directory / "case")})),
        )
        conn.commit()
        results.append(CampaignCaseResult(scenario_id, run_id, "queued"))
        jobs.log_event(conn, job_id, f"queued {run_id} (dp={case_meta['dp_m']} m)")

    jobs.update_payload(conn, job_id, run_ids=[r.run_id for r in results if r.status == "queued"])
    jobs.set_progress(conn, job_id, 0, sum(r.status == "queued" for r in results), "runs")

    return job_id, results


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_id")
    parser.add_argument("--model", choices=["dflowfm", "sph"], default="dflowfm")
    parser.add_argument("--extra", action="append", default=[], metavar="SCENARIO_ID",
                         help="include a named/historical scenario from the M5 design (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="generate/register cases without launching the solver")
    parser.add_argument("--demo", action="store_true", help="four short D-Flow FM scenarios for demo training")
    parser.add_argument("--data-dir", type=Path, help="override data root (also sets SIH26_DATA_DIR)")
    parser.add_argument("--sites-dir", type=Path, help="directory containing site YAML files")
    parser.add_argument("--production", action="store_true",
                        help="Feature 13: refuse unless the production-readiness gate is READY")
    args = parser.parse_args(argv)

    if args.data_dir is not None:
        os.environ["SIH26_DATA_DIR"] = str(args.data_dir.resolve())

    registry.init_db()
    conn = registry.connect()
    try:
        if args.model == "dflowfm":
            job_id, results = run_dflowfm_campaign(args.site_id, conn, sites_dir=args.sites_dir, extra=args.extra,
                                                    dry_run=args.dry_run, demo=args.demo, production=args.production)
        elif args.model == "sph":
            job_id, results = run_sph_campaign(args.site_id, conn)
        else:
            raise ValueError("ANUGA is not an M0 registry model; use dflowfm or sph")
    finally:
        conn.close()

    if job_id is None:
        for r in results:
            print(f"  {r.status:9s} {r.run_id or r.scenario_id}" + (f" -- {r.reason}" if r.reason else ""))
        if any(r.status == "blocked" for r in results):
            return 2
        print(f"{args.site_id}: no campaign scenarios queued")
        return 0
    print(f"{args.site_id}: campaign job {job_id}")
    for r in results:
        print(f"  {r.status:8s} {r.run_id}" + (f" -- {r.reason}" if r.reason else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

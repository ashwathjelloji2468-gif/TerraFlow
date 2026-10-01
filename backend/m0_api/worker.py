"""M0 job worker: a separate process that moves jobs through their stages.

    python -m backend.m0_api.worker            # alongside `uvicorn backend.m0_api.main:app`

The API only creates jobs and reads their status from `data/registry.sqlite`.
This process does the work, one job at a time (oldest first):

- **In-process stages** (`terrain`, `breach`, `design`, `training`,
  `validating`, `postprocessing`) are FAKE for now. Each sleeps
  `SIH26_FAKE_STAGE_S` seconds and then advances. M1, M2 and M5 plug in here later.
- **`simulating`** registers the job's runs, then launches them ONE AT A TIME
  (CLAUDE.md rule 13) as detached processes (rule 14). Each tick reads the
  running run's `log.txt`. The solver is currently `backend.m0_api.fake_solver`;
  the number of runs (`SIH26_FAKE_N_RUNS`) and its timings are fake settings,
  not design choices.
- **`recheck`'s `checking` stage is real**: it runs M7's lake-area check
  (`backend.m7_gee.fetch.run`) plus a library-age check against
  `site_status.DEFAULT_MAX_LIBRARY_AGE_DAYS`, and records the result in
  `data/<site_id>/site_status.json` (`_run_recheck`). Every tick, `_schedule_rechecks()`
  queues a `recheck` job for any known site whose `site_status` schedule is due and has
  no active job -- there is no separate timer/cron.

`tick()` does one short, non-blocking step. A long run is never waited on; it
is polled on the next tick. Because all state lives in SQLite and the run
logs, the API and the worker can each be restarted independently.
`recover()` runs at start-up and reconciles runs that were `running` when the
previous worker stopped. There are no retries yet: `max_run_retries` is still
unset in docs/decisions.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from backend.m0_api import jobs, registry, runner, site_status
from backend.m0_api import onboarding, site_registry
from backend.m3_dflowfm import launcher as m3_launcher
from backend.m7_gee import fetch as gee_fetch

log = logging.getLogger("m0.worker")

SIM_STAGES = {"simulating"}



class WorkerAlreadyRunning(RuntimeError):
    """Another worker holds `data/worker.lock`."""


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


class Worker:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection = registry.connect()
        self._lock_file = None
        self._procs: dict[str, subprocess.Popen] = {}  # runs this worker launched, so it can reap them

    # --- lifecycle -----------------------------------------------------------
    def acquire_lock(self) -> None:
        path = registry.data_dir() / "worker.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise WorkerAlreadyRunning(f"another worker holds {path}") from None
        f.write(str(os.getpid()))
        f.flush()
        self._lock_file = f

    def close(self) -> None:
        """Stop tracking everything. Detached runs keep going."""
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
        self.conn.close()

    def recover(self) -> None:
        """Reconcile state left behind by a previous worker.

        - A job in an in-process stage needs nothing: the next tick re-runs
          that stage from the start. The fake tasks are idempotent.
        - A run marked `running`: if its log says DONE, record the outcome.
          If the process is still alive, re-attach (later ticks keep polling).
          Otherwise it died unobserved, so the job fails with `worker_lost_run`.
        """
        for row in self._active_jobs():
            if row["stage"] == "queued":
                continue
            jobs.log_event(self.conn, row["job_id"], f"worker restarted; resuming at {row['stage']}")
            for run in jobs.job_runs(self.conn, row):
                if run["status"] != "running":
                    continue
                pid = json.loads(run["meta_json"] or "{}").get("pid")
                run_meta = json.loads(run["meta_json"] or "{}")
                if run_meta.get("solver") == "dflowfm":
                    case_dir = run_meta["case_dir"]
                    alive = pid is not None and m3_launcher.is_alive(pid, case_dir)
                    result = m3_launcher.check_success(case_dir, run_meta["model_stem"])
                    if result["success"]:
                        self._finish_run(row, run, True)
                    elif not alive:
                        self._retry_or_fail(row, run, "D-Flow FM process ended during worker restart")
                elif run_meta.get("solver") == "sph":
                    from backend.m4_sph import launcher as sph_launcher
                    alive = pid is not None and runner.is_alive(pid, run["run_id"])
                    result = sph_launcher.read_result(run["run_dir"], int(run_meta.get("attempt", 0)))
                    if result is not None and not alive:
                        self._finish_run(row, run, bool(result.get("success")))
                    elif not alive:
                        self._retry_or_fail(row, run, "DualSPHysics launcher ended without an execution record")
                else:
                    alive = pid is not None and runner.is_alive(pid, run["run_id"])
                    progress = runner.read_progress(runner.log_path(run["run_dir"]))
                    if progress.finished:
                        self._finish_run(row, run, progress.ok)
                    elif not alive:
                        self._mark_run(run["run_id"], "failed", error="process gone without DONE line")
                        jobs.fail(
                            self.conn, row["job_id"], row["stage"], "worker_lost_run",
                            f"Run {run['run_id']} stopped while no worker was watching it.",
                            {"run_id": run["run_id"], "pid": pid},
                        )
                if alive:
                    jobs.log_event(self.conn, row["job_id"], f"re-attached to run {run['run_id']} (pid {pid})")

    def run_forever(self, poll_s: float = 1.0) -> None:
        self.acquire_lock()
        self.recover()
        log.info("worker started (pid %s), registry %s", os.getpid(), registry.db_path())
        while True:
            if not self.tick():
                time.sleep(poll_s)

    # --- one step ------------------------------------------------------------
    def tick(self) -> bool:
        """Advance the oldest active job by one step. Returns False when there
        was nothing to do (idle, or only waiting on a running solver)."""
        self._reap()
        scheduled = self._schedule_rechecks()
        active = self._active_jobs()
        if not active:
            return scheduled
        row = active[0]
        kind, stage, job_id = row["kind"], row["stage"], row["job_id"]
        if kind not in jobs.STAGES:
            jobs.fail(self.conn, job_id, stage, "unsupported_job_kind",
                      f"Job kind {kind!r} has no stages in contract §5.3 yet.")
            return True

        if stage == "queued":
            jobs.update_payload(self.conn, job_id, started_at=registry.utc_now())
            self._advance(row)
            return True
        if stage in SIM_STAGES:
            stage_payload = jobs.payload(row)
            if kind == "onboarding" and stage_payload.get("i1_synthetic"):
                jobs.log_event(self.conn, job_id, "synthetic demo run artifacts ready; no solver was executed")
                self._advance(row)
                return True
            return self._tick_simulating(row)
        if kind == "recheck" and stage == "checking":
            self._run_recheck(row)
            self._advance(row)
            return True

        # API-created onboarding jobs always carry the contract-validated JSON
        # SiteConfig. Keep the orchestration here thin: module work stays in
        # M1, M2 and M5, with products exchanged through their normal files.
        payload = jobs.payload(row)
        if kind == "onboarding" and isinstance(payload.get("site_config"), dict):
            data_dir = Path(payload.get("data_dir") or registry.data_dir())
            try:
                if stage == "terrain":
                    if payload.get("i1_synthetic"):
                        from backend.m0_api.synthetic_demo import create_site_artifacts
                        onboarding.materialize_site_config(row["site_id"], payload["site_config"], data_dir)
                        create_site_artifacts(row["site_id"], data_dir)
                        result = data_dir / row["site_id"] / "terrain/grid.json"
                    else:
                        result = onboarding.prepare_terrain(
                            row["site_id"], data_dir, payload["site_config"],
                            event=lambda msg, job_id=row["job_id"]: jobs.log_event(self.conn, job_id, msg))
                elif stage == "breach" and payload.get("i1_synthetic"):
                    result = True
                elif stage == "design" and payload.get("i1_synthetic"):
                    result = True
                elif stage == "training" and payload.get("i1_synthetic"):
                    result = True
                elif stage == "breach":
                    result = onboarding.prepare_breach(
                        row["site_id"], data_dir,
                        event=lambda msg, job_id=row["job_id"]: jobs.log_event(self.conn, job_id, msg))
                elif stage == "design":
                    result = onboarding.prepare_design(row["site_id"], data_dir, demo=bool(payload.get("demo_mode")))
                elif stage == "training":
                    from backend.m5_emulator.train_site import train_site
                    result = train_site(row["site_id"], data_dir, demo=bool(payload.get("demo_mode")))
                else:
                    result = None
                if result is not None:
                    jobs.log_event(self.conn, row["job_id"], f"{stage} complete")
                    self._advance(row)
                    return True
            except Exception as exc:
                # Modules may attach their own contract §2.7 code (e.g. Feature 2's
                # `terrain_inputs_incomplete`); otherwise the generic `<stage>_failed`.
                jobs.fail(self.conn, row["job_id"], stage,
                          getattr(exc, "job_error_code", f"{stage}_failed"), str(exc),
                          getattr(exc, "job_error_details", None))
                return True

        time.sleep(_env_float("SIH26_FAKE_STAGE_S", 3.0))  # FAKE stage work
        self._advance(row)
        return True

    # --- scheduled re-checks ---------------------------------------------------
    def _has_published_library(self, site_id: str) -> bool:
        """A re-check only makes sense once there is a library to go stale (docs/decisions.md
        2.1/2.3: `outdated` applies to a site whose job already reached `ready`)."""
        data_dir = registry.data_dir()
        return any((data_dir / site_id / "emulator" / model / "manifest.json").is_file()
                   for model in ("delft3d", "sph"))

    def _schedule_rechecks(self) -> bool:
        """Queue a `recheck` job for every registered site whose schedule is due (or has never been
        checked) and that has no active job already. Runs every tick; cheap (a handful of file
        stats), so no separate timer is needed."""
        queued_any = False
        for site_id in site_registry.list_site_ids():
            if jobs.find_active_job(self.conn, site_id) is not None:
                continue
            if not self._has_published_library(site_id):
                continue
            if not site_status.is_due(site_status.load(site_id)):
                continue
            job_id = jobs.create_job(self.conn, "recheck", site_id)
            jobs.log_event(self.conn, job_id, f"scheduled re-check for '{site_id}'")
            queued_any = True
        return queued_any

    def _run_recheck(self, row: sqlite3.Row) -> None:
        """The `checking` stage's real work: M7's lake-area check plus a library-age check.
        Never raises -- a re-check that can't complete this tick (e.g. Earth Engine and cache both
        unavailable) just leaves the site's outdated flag as it was and tries again next schedule."""
        site_id, job_id = row["site_id"], row["job_id"]
        data_dir = registry.data_dir()
        outdated, reason_key, detail = False, None, None

        try:
            provider = gee_fetch.best_effort_provider()
            gee_fetch.run(site_id, provider=provider, data_dir=data_dir)
            recheck = json.loads((data_dir / site_id / "gee" / "recheck.json").read_text())
            if recheck.get("outdated"):
                outdated, reason_key = True, "outdated_lake_area_change"
                detail = {"change_pct": recheck["change_pct"], "threshold_pct": recheck["threshold_pct"],
                          "checked_at": recheck["checked_at"]}
        except Exception as e:
            jobs.log_event(self.conn, job_id, f"lake-area check failed, skipping: {e}")

        if not outdated:
            for model in ("delft3d", "sph"):
                manifest_path = data_dir / site_id / "emulator" / model / "manifest.json"
                if not manifest_path.is_file():
                    continue
                trained_at = datetime.fromisoformat(
                    json.loads(manifest_path.read_text())["trained_at"].replace("Z", "+00:00")
                )
                age_days = (registry.utc_now_dt() - trained_at).total_seconds() / 86400.0
                if age_days > site_status.DEFAULT_MAX_LIBRARY_AGE_DAYS:
                    outdated, reason_key = True, "outdated_library_age"
                    detail = {"trained_at": json.loads(manifest_path.read_text())["trained_at"],
                              "age_days": round(age_days, 1),
                              "max_age_days": site_status.DEFAULT_MAX_LIBRARY_AGE_DAYS}
                    break

        site_status.record_check(site_id, outdated, reason_key, detail, data_dir=data_dir)
        jobs.log_event(self.conn, job_id, f"re-check done: outdated={outdated} reason={reason_key}")

    # --- simulating ----------------------------------------------------------
    def _tick_simulating(self, row: sqlite3.Row) -> bool:
        job_id = row["job_id"]
        if not jobs.payload(row).get("run_ids"):
            if jobs.payload(row).get("sph_campaign"):
                self._advance(row)
                return True
            self._register_runs(row)
            return True
        row = jobs.get_job(self.conn, job_id)
        runs = jobs.job_runs(self.conn, row)

        running = [r for r in runs if r["status"] == "running"]
        if running:
            return self._poll_run(row, running[0])

        queued = [r for r in runs if r["status"] == "queued"]
        if queued:
            self._launch(row, queued[0])
            return True

        failed = [r for r in runs if r["status"] == "failed"]
        if failed and (jobs.payload(row).get("dflowfm_campaign") or jobs.payload(row).get("sph_campaign")):
            jobs.fail(self.conn, job_id, row["stage"], "run_failed",
                      f"Campaign contains failed run {failed[0]['run_id']}.",
                      {"run_id": failed[0]["run_id"], "error": failed[0]["error"]})
            from backend.campaign import write_dflowfm_status
            write_dflowfm_status(self._campaign_data_dir(row), row["site_id"], self.conn, job_id)
            return True

        self._advance(row)  # every run completed (a failed run already failed the job)
        return True

    def _register_runs(self, row: sqlite3.Row) -> None:
        """Create the job's scenarios + runs in the registry (FAKE design: no params)."""
        site_id, job_id = row["site_id"], row["job_id"]
        demo = bool(jobs.payload(row).get("demo_mode"))
        kind, prefix = ("demo", f"{site_id}_demo_s") if demo else ("design", f"{site_id}_s")
        n_existing = self.conn.execute(
            "SELECT COUNT(*) FROM scenarios WHERE site_id = ? AND kind = ?", (site_id, kind)
        ).fetchone()[0]
        n = 1 if jobs.payload(row).get("case_dir") else _env_int("SIH26_FAKE_N_RUNS", 3)
        now = registry.utc_now()
        run_ids = []
        with self.conn:
            for i in range(n_existing + 1, n_existing + n + 1):
                scenario_id = f"{prefix}{i:03d}"  # contract §1.7; numbers are never reused
                run_id = f"{scenario_id}__delft3d"
                self.conn.execute(
                    "INSERT INTO scenarios (scenario_id, site_id, kind, params_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (scenario_id, site_id, kind, json.dumps({"fake": True}), now),
                )
                self.conn.execute(
                    "INSERT INTO runs (run_id, scenario_id, model, status, run_dir) VALUES (?, ?, 'delft3d', 'queued', ?)",
                    (run_id, scenario_id, str(jobs.run_dir(site_id, run_id))),
                )
                run_ids.append(run_id)
        jobs.update_payload(self.conn, job_id, run_ids=run_ids)
        jobs.set_progress(self.conn, job_id, 0, n, "runs")
        jobs.log_event(self.conn, job_id, f"registered {n} runs")

    def _launch(self, row: sqlite3.Row, run: sqlite3.Row) -> None:
        run_id = run["run_id"]
        index = jobs.payload(row)["run_ids"].index(run_id) + 1
        job_payload = jobs.payload(row)
        current_meta = json.loads(run["meta_json"] or "{}")
        case_dir = current_meta.get("case_dir", job_payload.get("case_dir"))
        if job_payload.get("sph_campaign"):
            from backend.m4_sph import launcher as sph_launcher
            attempt = int(current_meta.get("attempt", 0))
            proc = sph_launcher.launch_case(case_dir, run["run_dir"], job_payload.get("binaries_dir"), attempt)
            solver_meta = {"solver": "sph", "case_dir": str(Path(case_dir).resolve()), "attempt": attempt}
        elif case_dir:
            model = job_payload.get("model_file", "model.mdu") if job_payload.get("dflowfm_campaign") else job_payload.get("model", "model.mdu")
            proc = m3_launcher.launch_case(case_dir, run["run_dir"], model=model)
            model_stem = Path(model).stem
            solver_meta = {"solver": "dflowfm", "case_dir": str(Path(case_dir).resolve()),
                           "model_stem": model_stem}
        else:
            cmd = [
                sys.executable, "-m", "backend.m0_api.fake_solver",
                "--run-id", run_id,
                "--steps", str(_env_int("SIH26_FAKE_RUN_STEPS", 5)),
                "--step-s", str(_env_float("SIH26_FAKE_RUN_STEP_S", 2.0)),
            ]
            if os.environ.get("SIH26_FAKE_FAIL_RUN") == str(index):  # test hook: make run N fail
                cmd += ["--fail-at", "1"]
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(registry.REPO_ROOT), os.environ.get("PYTHONPATH")])))
            proc = runner.launch_detached(cmd, run["run_dir"], env=env)
            solver_meta = {}
        self._procs[run_id] = proc
        started = time.time()
        current_meta.update({"pid": proc.pid, "started_epoch_s": started, **solver_meta})
        self._mark_run(run_id, "running", meta=current_meta, started_at=registry.utc_now())
        jobs.log_event(self.conn, row["job_id"], f"run {run_id} started (pid {proc.pid})")
        if job_payload.get("dflowfm_campaign"):
            from backend.campaign import write_dflowfm_status
            write_dflowfm_status(self._campaign_data_dir(row), row["site_id"], self.conn, row["job_id"])

    def _poll_run(self, row: sqlite3.Row, run: sqlite3.Row) -> bool:
        pid = json.loads(run["meta_json"] or "{}").get("pid")
        meta = json.loads(run["meta_json"] or "{}")
        if meta.get("solver") == "dflowfm":
            case_dir, model_stem = meta["case_dir"], meta["model_stem"]
            progress = m3_launcher.read_progress(case_dir, model_stem)
            alive = pid is not None and m3_launcher.is_alive(pid, case_dir)
            if progress.steps_total:
                jobs.set_progress(self.conn, row["job_id"], progress.steps_done,
                                  progress.steps_total, "s")
            if progress.steps_done and meta.get("last_dflow_time_s") != progress.steps_done:
                meta["last_dflow_time_s"] = progress.steps_done
                self._mark_run(run["run_id"], "running", meta=meta)
                jobs.log_event(self.conn, row["job_id"], f"D-Flow FM simulation time {progress.steps_done} s")
            # A successful .dia and output file can appear before D-Flow has
            # closed its NetCDF writers. Do not postprocess a live solver.
            if progress.ok and not alive:
                self._finish_run(row, run, True)
            elif not alive:
                result = m3_launcher.check_success(case_dir, model_stem)
                self._retry_or_fail(row, run, "D-Flow FM failed M3 rule 1", result)
            return not (progress.ok and not alive)
        if meta.get("solver") == "sph":
            from backend.m4_sph import launcher as sph_launcher
            alive = pid is not None and runner.is_alive(pid, run["run_id"])
            result = sph_launcher.read_result(run["run_dir"], int(meta.get("attempt", 0)))
            if result is not None and not alive:
                if meta.get("started_epoch_s") is not None:
                    result["wall_time_s"] = round(time.time() - meta["started_epoch_s"], 3)
                meta["execution"] = result
                self._mark_run(run["run_id"], "running", meta=meta)
                refreshed = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
                self._finish_run(row, refreshed, bool(result.get("success")))
                return True
            if not alive:
                self._retry_or_fail(row, run, "DualSPHysics launcher ended without an execution record")
                return True
            return False
        alive = pid is not None and runner.is_alive(pid, run["run_id"])  # check before reading the log (no race)
        progress = runner.read_progress(runner.log_path(run["run_dir"]))
        if progress.finished:
            self._finish_run(row, run, progress.ok)
            return True
        if not alive:
            self._retry_or_fail(row, run, "process exited without DONE line")
            return True
        return False  # still running; nothing changed

    def _finish_run(self, row: sqlite3.Row, run: sqlite3.Row, ok: bool) -> None:
        meta = json.loads(run["meta_json"] or "{}")
        if "started_epoch_s" in meta:
            meta["wall_time_s"] = round(time.time() - meta["started_epoch_s"], 3)
        job_id = row["job_id"]
        payload = jobs.payload(jobs.get_job(self.conn, job_id))
        if ok:
            if payload.get("dflowfm_campaign"):
                try:
                    from backend.m3_common.postprocess import PostprocessConfig, postprocess_dflowfm
                    from backend.m5_emulator.run_cache import register_run
                    data_dir = self._campaign_data_dir(row)
                    case_dir = Path(meta["case_dir"])
                    terrain = data_dir / row["site_id"] / "terrain"
                    scenario_path = Path(run["run_dir"]) / "scenario.json"
                    scenario = json.loads(scenario_path.read_text())
                    grid_path = terrain / "grid.json"
                    post_meta = postprocess_dflowfm(case_dir, run["run_dir"], grid_path=grid_path,
                        domain_mask_path=terrain / "domain_mask.tif", run_id=run["run_id"],
                        scenario_id=run["scenario_id"], hydrographs=meta.get("hydrographs", []), spinup_s=7200.0,
                        config=PostprocessConfig(delete_raw_map=False))
                    from backend.shared.site_config import load_site_config
                    config = load_site_config(row["site_id"], sites_dir=jobs.payload(jobs.get_job(self.conn, job_id)).get("sites_dir"))
                    from backend.m3_dflowfm.generator import placeholder_fields
                    missing_facts = placeholder_fields(config)
                    post_meta["has_placeholders"] = bool(missing_facts)
                    post_meta["placeholder_fields"] = missing_facts
                    post_meta.setdefault("caveats", ["clear_water"])
                    if missing_facts and "placeholder_data" not in post_meta["caveats"]:
                        post_meta["caveats"].append("placeholder_data")
                    post_meta["wall_time_s"] = meta.get("wall_time_s")
                    (Path(run["run_dir"]) / "run_meta.json").write_text(json.dumps(post_meta, indent=2) + "\n")
                    register_run(data_dir, row["site_id"], run["run_id"], scenario["params"], post_meta)
                    meta.update(post_meta)
                    self._mark_run(run["run_id"], "postprocessed", meta=meta, finished_at=registry.utc_now())
                except Exception as exc:
                    self._retry_or_fail(row, run, f"post-processing/cache load failed: {exc}")
                    return
            elif payload.get("sph_campaign"):
                try:
                    from backend.m4_sph.postprocess import postprocess_run
                    data_dir = Path(payload.get("data_dir") or registry.data_dir())
                    terrain_dir = Path(payload.get("terrain_dir") or data_dir / row["site_id"] / "terrain")
                    execution = meta.get("execution") or {}
                    run_root = Path(run["run_dir"])
                    from backend.m4_sph.settings import load_sph_settings
                    sph_settings = load_sph_settings(**payload.get("sph_settings", {}))
                    run_meta = postprocess_run(run_root, terrain_dir, run_root / "raw" / "data",
                                               settings=sph_settings, binaries_dir=payload.get("binaries_dir"))
                    case_meta = json.loads((run_root / "case" / "case_meta.json").read_text())
                    for key in ("run_id", "label", "domain_status", "output_classification",
                                "source_m3_run_id", "routed_discharge_csv", "routed_discharge_manifest",
                                "terrain_provenance", "comparison_section_id", "provenance"):
                        if key in case_meta:
                            run_meta[key] = case_meta[key]
                    run_meta["output_classification"] = "REAL_SIMULATION_ARTIFACT"
                    run_meta["solver_status"] = "REAL_SOLVER_OUTPUT"
                    run_meta.update({
                        "wall_time_s": execution.get("wall_time_s", meta.get("wall_time_s")),
                        "peak_vram_mb": execution.get("peak_vram_mb"),
                        "started_at": execution.get("started_at", run_meta["started_at"]),
                        "finished_at": execution.get("finished_at", run_meta["finished_at"]),
                        "execution": execution,
                        "artifact_paths": {"case": meta["case_dir"], "raw": str(run_root / "raw"),
                                           "run_meta": str(run_root / "run_meta.json"),
                                           "summary_nearfield": str(run_root / "summary_nearfield"),
                                           **({"timeseries": str(run_root / "timeseries.csv")}
                                              if (run_root / "timeseries.csv").is_file() else {}),
                                           "surfaces": str(run_root / "surfaces")},
                    })
                    (run_root / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n")
                    meta.update(run_meta)
                    self._mark_run(run["run_id"], "postprocessed", meta=meta, finished_at=registry.utc_now())
                except Exception as exc:
                    self._retry_or_fail(row, run, f"SPH post-processing failed: {exc}")
                    return
            else:
                self._mark_run(run["run_id"], "completed", meta=meta, finished_at=registry.utc_now())
            current_payload = jobs.payload(jobs.get_job(self.conn, job_id))
            if current_payload.get("dflowfm_campaign") or current_payload.get("sph_campaign"):
                completed = sum(r["status"] == "postprocessed" for r in jobs.job_runs(self.conn, jobs.get_job(self.conn, job_id)))
            else:
                completed = sum(r["status"] == "completed" for r in jobs.job_runs(self.conn, jobs.get_job(self.conn, job_id)))
            jobs.set_progress(self.conn, job_id, completed, row["progress_total"], "runs")
            jobs.log_event(self.conn, job_id, f"run {run['run_id']} completed")
        else:
            if payload.get("dflowfm_campaign") or payload.get("sph_campaign"):
                self._retry_or_fail(row, run, "solver reported failure", {"solver_result": meta.get("execution")})
                return
            self._mark_run(run["run_id"], "failed", meta=meta, finished_at=registry.utc_now(), error="solver reported failure")
            if not (jobs.payload(jobs.get_job(self.conn, job_id)).get("dflowfm_campaign") or
                    jobs.payload(jobs.get_job(self.conn, job_id)).get("sph_campaign")):
                jobs.fail(self.conn, job_id, row["stage"], "run_failed",
                          f"Run {run['run_id']} reported failure.", {"run_id": run["run_id"]})

        payload = jobs.payload(jobs.get_job(self.conn, job_id))
        if payload.get("dflowfm_campaign"):
            from backend.campaign import write_dflowfm_status
            write_dflowfm_status(self._campaign_data_dir(row), row["site_id"], self.conn, job_id)

    def _retry_or_fail(self, row: sqlite3.Row, run: sqlite3.Row, message: str, details: dict | None = None) -> None:
        """Persist one retry before making a run/job terminally failed."""
        meta = json.loads(run["meta_json"] or "{}")
        payload = jobs.payload(row)
        is_solver_campaign = payload.get("dflowfm_campaign") or payload.get("sph_campaign")
        if is_solver_campaign and int(meta.get("attempt", 0)) < 1:
            if payload.get("sph_campaign"):
                from backend.m4_sph import launcher as sph_launcher
                sph_launcher.archive_failed_output(run["run_dir"], int(meta.get("attempt", 0)))
            meta["attempt"] = int(meta.get("attempt", 0)) + 1
            meta.pop("pid", None)
            meta.pop("started_epoch_s", None)
            self._mark_run(run["run_id"], "queued", meta=meta, error=None)
            jobs.log_event(self.conn, row["job_id"], f"retrying {run['run_id']} once: {message}")
        else:
            meta.update({"failure_reason": message, **(details or {})})
            if is_solver_campaign:
                meta.update({"solver_status": "FAILED", "output_classification": "FAILED_SOLVER_ATTEMPT"})
            self._mark_run(run["run_id"], "failed", meta=meta, error=message, finished_at=registry.utc_now())
            if not is_solver_campaign:
                jobs.fail(self.conn, row["job_id"], row["stage"], "run_failed",
                          f"Run {run['run_id']} failed: {message}", {"run_id": run["run_id"], **(details or {})})
        if jobs.payload(jobs.get_job(self.conn, row["job_id"])).get("dflowfm_campaign"):
            from backend.campaign import write_dflowfm_status
            write_dflowfm_status(self._campaign_data_dir(row), row["site_id"], self.conn, row["job_id"])

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _campaign_data_dir(row: sqlite3.Row) -> Path:
        root = jobs.payload(row).get("data_dir")
        return Path(root) if root else registry.data_dir()

    def _active_jobs(self) -> list[sqlite3.Row]:
        terminal = sorted(jobs.TERMINAL)
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE stage NOT IN ({','.join('?' * len(terminal))}) ORDER BY created_at, job_id",
            terminal,
        ).fetchall()

    def _advance(self, row: sqlite3.Row) -> None:
        new = jobs.next_stage(row["kind"], row["stage"])
        jobs.set_stage(self.conn, row["job_id"], row["stage"], new)
        payload = jobs.payload(jobs.get_job(self.conn, row["job_id"]))
        if row["kind"] == "onboarding" and payload.get("i1_synthetic") and new == "ready":
            data_dir = Path(payload.get("data_dir") or registry.data_dir())
            marker = data_dir / row["site_id"] / "demo_ready.json"
            marker.write_text(json.dumps({"synthetic": True, "demo": True, "created_at": registry.utc_now()}))
        if row["stage"] in SIM_STAGES:  # run counts describe `simulating` only
            jobs.set_progress(self.conn, row["job_id"], None, None, None)
        if new in SIM_STAGES:  # so `simulating` never shows without its 0/N
            payload = jobs.payload(jobs.get_job(self.conn, row["job_id"]))
            if row["kind"] == "onboarding" and isinstance(payload.get("site_config"), dict):
                data_dir = Path(payload.get("data_dir") or registry.data_dir())
                try:
                    if payload.get("i1_synthetic"):
                        jobs.update_payload(self.conn, row["job_id"], dflowfm_campaign=True)
                    else:
                        from backend.campaign import run_dflowfm_campaign
                        run_dflowfm_campaign(
                            row["site_id"], self.conn, data_dir=data_dir,
                            sites_dir=data_dir / row["site_id"] / "config",
                            demo=bool(payload.get("demo_mode")), job_id=row["job_id"],
                        )
                except Exception as exc:
                    current = jobs.get_job(self.conn, row["job_id"])
                    jobs.fail(self.conn, row["job_id"], current["stage"], "campaign_prepare_failed", str(exc))
            elif not payload.get("dflowfm_campaign") and not payload.get("sph_campaign"):
                self._register_runs(jobs.get_job(self.conn, row["job_id"]))
        log.info("job %s: %s -> %s", row["job_id"], row["stage"], new)

    def _mark_run(self, run_id: str, status: str, meta: dict | None = None, started_at: str | None = None,
                  finished_at: str | None = None, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = ?,"
                " meta_json = COALESCE(?, meta_json), started_at = COALESCE(?, started_at),"
                " finished_at = COALESCE(?, finished_at), error = COALESCE(?, error)"
                " WHERE run_id = ?",
                (status, json.dumps(meta) if meta is not None else None, started_at, finished_at, error, run_id),
            )

    def _reap(self) -> None:
        """Collect exit codes of runs this worker launched, so they don't linger as zombies."""
        for run_id, proc in list(self._procs.items()):
            if proc.poll() is not None:
                del self._procs[run_id]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M0 job worker")
    parser.add_argument("--poll-s", type=float, default=1.0, help="sleep between idle ticks")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        Worker().run_forever(args.poll_s)
    except WorkerAlreadyRunning as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("worker stopped; detached runs keep going and will be picked up on restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.m0_api import jobs, registry
from backend.m0_api.worker import Worker
from tests.m0_api.conftest import wait_until


def _fake_binaries(path: Path) -> Path:
    path.mkdir()
    gen = path / "GenCase_linux64"
    gen.write_text("#!/bin/sh\nset -eu\nprintf '<case/>\\n' > \"$2.xml\"\nprintf init > \"$2.bi4\"\n", encoding="utf-8")
    gen.chmod(0o755)
    solver = path / "DualSPHysics5.4_linux64"
    solver.write_text("#!/bin/sh\nset -eu\nmkdir -p \"$5/data\"\nprintf out > \"$5/Run.out\"\nprintf part > \"$5/data/Part_0000.bi4\"\necho 'Finished execution (code=0).'\n", encoding="utf-8")
    solver.chmod(0o755)
    return path


def test_campaign_worker_runs_sph_and_registers_artifact_paths(tmp_path, monkeypatch, data_dir):
    from backend.m4_sph import postprocess

    bin_dir = _fake_binaries(tmp_path / "bin")
    run_id, scenario_id, site_id = "synth__s001__sph", "synth__s001", "synth"
    run_dir = data_dir / site_id / "runs" / run_id
    case_dir = run_dir / "case"
    case_dir.mkdir(parents=True)
    (case_dir / "synth__s001__sph_Def.xml").write_text("<case/>", encoding="utf-8")
    (case_dir / "case_meta.json").write_text(json.dumps({"scenario_id": scenario_id, "dp_m": 0.02}), encoding="utf-8")

    def controlled_postprocess(run_root, terrain_dir, dirdata, settings=None, binaries_dir=None, execution=None):
        meta = {"contract_version": "0.3.0", "run_id": run_id, "scenario_id": scenario_id, "model": "sph",
                "status": "postprocessed", "solver_version": "controlled-test", "resolution_m": 1.0,
                "dp_m": 0.02, "particle_count": 1, "peak_vram_mb": None, "sim_duration_s": 1.0,
                "wall_time_s": None, "mass_balance_error_pct": None,
                "thresholds": {"extent_m": 0.3, "arrival_m": 0.1}, "hydrographs": [], "resampling": "test",
                "warnings": [], "caveats": [], "started_at": "2026-09-27T00:00:00+00:00",
                "finished_at": "2026-09-27T00:00:01+00:00"}
        (Path(run_root) / "run_meta.json").write_text(json.dumps(meta))
        return meta

    monkeypatch.setattr(postprocess, "postprocess_run", controlled_postprocess)
    conn = registry.connect()
    job_id = jobs.create_job(conn, "campaign", site_id, payload={
        "model": "sph", "sph_campaign": True, "data_dir": str(data_dir),
        "binaries_dir": str(bin_dir),
    })
    jobs.update_payload(conn, job_id, run_ids=[run_id])
    with conn:
        conn.execute("INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)",
                     (scenario_id, site_id, "design", json.dumps({"test": True}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json) VALUES (?, ?, 'sph', 'queued', ?, ?)",
                     (run_id, scenario_id, str(run_dir), json.dumps({"case_dir": str(case_dir), "attempt": 0})))
    jobs.set_progress(conn, job_id, 0, 1, "runs")
    jobs.set_stage(conn, job_id, "queued", "simulating")
    conn.close()

    worker = Worker()
    try:
        wait_until(lambda: (worker.conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                           in {"postprocessed", "failed"}), worker.tick)
        state = worker.conn.execute("SELECT status, error FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert state["status"] == "postprocessed", state["error"]
    finally:
        worker.close()

    conn = registry.connect()
    try:
        row = conn.execute("SELECT status, meta_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        meta = json.loads(row["meta_json"])
        assert row["status"] == "postprocessed"
        assert (run_dir / "raw/Run.out").stat().st_size > 0
        assert (run_dir / "raw/data/Part_0000.bi4").stat().st_size > 0
        assert meta["execution"]["solver_returncode"] == 0
        assert meta["artifact_paths"]["raw"] == str(run_dir / "raw")
        assert (run_dir / "run_meta.json").is_file()
        assert jobs.get_job(conn, job_id)["stage"] == "simulating"
    finally:
        conn.close()


def test_sph_campaign_solver_failure_queues_one_retry_and_preserves_raw(tmp_path, data_dir):
    run_id, scenario_id, site_id = "synth__retry__sph", "synth__retry", "synth"
    run_dir = data_dir / site_id / "runs" / run_id
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "raw" / "Run.out").write_text("failed attempt evidence")
    conn = registry.connect()
    job_id = jobs.create_job(conn, "campaign", site_id, payload={"sph_campaign": True})
    jobs.update_payload(conn, job_id, run_ids=[run_id])
    with conn:
        conn.execute("INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)",
                     (scenario_id, site_id, "named", "{}", registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json)"
                     " VALUES (?, ?, 'sph', 'running', ?, ?)",
                     (run_id, scenario_id, str(run_dir), json.dumps({"attempt": 0})))
    jobs.set_progress(conn, job_id, 0, 1, "runs")
    jobs.set_stage(conn, job_id, "queued", "simulating")
    worker = Worker()
    try:
        row = jobs.get_job(worker.conn, job_id)
        run = worker.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        worker._finish_run(row, run, False)
        updated = worker.conn.execute("SELECT status, meta_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert updated["status"] == "queued"
        assert json.loads(updated["meta_json"])["attempt"] == 1
        assert (run_dir / "attempts/a00/failed_raw/Run.out").read_text() == "failed attempt evidence"
    finally:
        worker.close()
        conn.close()


@pytest.mark.skipif(not os.environ.get("DSPH_BIN_DIR"), reason="set DSPH_BIN_DIR for the controlled DualSPHysics worker test")
def test_registered_campaign_job_executes_stock_sph_and_registers_outputs(tmp_path, monkeypatch, data_dir):
    from backend.m4_sph import postprocess

    source = Path(__file__).resolve().parents[2] / "backend/m4_pilot/dambreak3d_dp0p0200/CaseDambreak_Def.xml"
    run_id, scenario_id, site_id = "synth__controlled__sph", "synth__controlled", "synth"
    run_dir = data_dir / site_id / "runs" / run_id
    case_dir = run_dir / "case"
    case_dir.mkdir(parents=True)
    (case_dir / source.name).write_bytes(source.read_bytes())
    (case_dir / "case_meta.json").write_text(json.dumps({"scenario_id": scenario_id, "dp_m": 0.02}), encoding="utf-8")

    def controlled_postprocess(run_root, terrain_dir, dirdata, settings=None, binaries_dir=None, execution=None):
        meta = {"contract_version": "0.3.0", "run_id": run_id, "scenario_id": scenario_id, "model": "sph",
                "status": "postprocessed", "solver_version": "DualSPHysics 5.4.355", "resolution_m": None,
                "dp_m": 0.02, "particle_count": 17446, "peak_vram_mb": None, "sim_duration_s": 2.0,
                "wall_time_s": None, "mass_balance_error_pct": None,
                "thresholds": {"extent_m": 0.3, "arrival_m": 0.1}, "hydrographs": [], "resampling": "controlled stock pilot",
                "warnings": ["controlled stock case has no georeferenced site terrain"], "caveats": ["synthetic_world_not_real_physics"],
                "started_at": "2026-09-27T00:00:00+00:00", "finished_at": "2026-09-27T00:00:01+00:00"}
        (Path(run_root) / "run_meta.json").write_text(json.dumps(meta))
        return meta

    monkeypatch.setattr(postprocess, "postprocess_run", controlled_postprocess)
    conn = registry.connect()
    job_id = jobs.create_job(conn, "campaign", site_id, payload={
        "model": "sph", "sph_campaign": True, "data_dir": str(data_dir),
        "binaries_dir": os.environ["DSPH_BIN_DIR"],
    })
    jobs.update_payload(conn, job_id, run_ids=[run_id])
    with conn:
        conn.execute("INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)",
                     (scenario_id, site_id, "design", json.dumps({"controlled": True}), registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json) VALUES (?, ?, 'sph', 'queued', ?, ?)",
                     (run_id, scenario_id, str(run_dir), json.dumps({"case_dir": str(case_dir), "attempt": 0})))
    jobs.set_progress(conn, job_id, 0, 1, "runs")
    jobs.set_stage(conn, job_id, "queued", "simulating")
    conn.close()

    worker = Worker()
    try:
        wait_until(lambda: worker.conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                   in {"postprocessed", "failed"}, worker.tick, timeout_s=45)
        state = worker.conn.execute("SELECT status, meta_json, error FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert state["status"] == "postprocessed", state["error"]
        registered = json.loads(state["meta_json"])
    finally:
        worker.close()

    execution = registered["execution"]
    assert execution["solver_returncode"] == 0
    assert execution["peak_vram_mb"] is not None
    assert (run_dir / "raw/Run.out").stat().st_size > 0
    assert list((run_dir / "raw/data").glob("Part_*.bi4"))
    assert registered["artifact_paths"]["raw"] == str(run_dir / "raw")
    assert (run_dir / "run_meta.json").is_file()

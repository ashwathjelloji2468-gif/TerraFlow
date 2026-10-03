from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import yaml

import backend.m3_dflowfm
from backend.campaign import run_dflowfm_campaign
from backend.m0_api import registry, jobs
from backend.m0_api.worker import Worker


import pytest


@pytest.mark.parametrize("accepted", [True, False])
def test_synthetic_dflowfm_campaign_retries_then_postprocesses_and_caches(tmp_path, monkeypatch, accepted):
    data_dir, sites_dir = tmp_path / "data", tmp_path / "sites"
    monkeypatch.setenv("SIH26_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIH26_FAKE_STAGE_S", "0")
    registry.init_db()
    source = Path(__file__).parent / "fixtures/shared/synth.yaml"
    raw = yaml.safe_load(source.read_text())
    raw = copy.deepcopy(raw)
    raw["domains"]["far_field"]["inflow"]["base_flow"] = {
        "value": 1.0, "unit": "m^3/s", "source": "synthetic fixture", "status": "placeholder"}
    sites_dir.mkdir()
    (sites_dir / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    design_dir = data_dir / "synth/design"
    design_dir.mkdir(parents=True)
    design = {"contract_version": "0.3.0", "site_id": "synth", "model": "delft3d",
        "method": "maximin_lhs", "seed": 7, "n": 1, "inputs": [],
        "scenarios": [{"scenario_id": "synth__s001", "kind": "design",
            "params": {"water_volume_m3": 1e6, "breach_width_m": 10.0, "failure_time_s": 100.0}}],
        "extra": [], "has_placeholders": True, "caveats": []}
    (design_dir / "scenario_design.json").write_text(json.dumps(design))

    fake_generator = types.ModuleType("backend.m3_dflowfm.generator")
    fake_generator.DEFAULT_STOP_S = 108000.0
    fake_generator.placeholder_fields = lambda config: ["domains.far_field.inflow.base_flow"]
    def build_case(site_id, scenario_id, params, *, data_dir, sites_dir, case_dir, stop_s, demo=False):
        case_dir = Path(case_dir)
        (case_dir / "output").mkdir(parents=True)
        (case_dir / "model.mdu").write_text("tStop = 9000\n")
        (case_dir / "case_meta.json").write_text(json.dumps({"spinup_s": 7200.0, "stop_s": 9000.0}))
        return case_dir, {"scenario_id": scenario_id, "sim_duration_s": stop_s}
    fake_generator.build_case = build_case
    monkeypatch.setitem(sys.modules, "backend.m3_dflowfm.generator", fake_generator)
    monkeypatch.setattr(backend.m3_dflowfm, "generator", fake_generator, raising=False)
    hydrograph_module = __import__("backend.m2_breach.hydrograph", fromlist=["hydrograph"])
    monkeypatch.setattr(hydrograph_module, "hydrograph", lambda *args, **kwargs: object())
    def fake_write_hydrograph(hg, site_id, scenario_id, data_dir):
        path = Path(data_dir) / site_id / "breach/hydrographs" / f"{scenario_id}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic hydrograph\n")
        return path, path.with_suffix(".json")
    monkeypatch.setattr(hydrograph_module, "write_hydrograph", fake_write_hydrograph)

    conn = registry.connect()
    job_id, cases = run_dflowfm_campaign("synth", conn, data_dir, sites_dir)
    assert cases[0].status == "queued"

    attempts = {"count": 0}
    def fake_launch(case_dir, run_dir, *, model):
        attempts["count"] += 1
        output = Path(case_dir) / "output"
        output.mkdir(parents=True, exist_ok=True)  # the real launch_case recreates output/ (Feature 17 archival)
        if attempts["count"] == 1:
            (output / "model.dia").write_text("** ERROR : fake first attempt\n")
        else:
            (output / "model.dia").write_text("** INFO : simulation period (s) : 9000\n")
            (output / "model_map.nc").touch()
            (output / "model_his.nc").touch()
        return SimpleNamespace(pid=40000 + attempts["count"], poll=lambda: 0)
    monkeypatch.setattr("backend.m0_api.worker.m3_launcher.launch_case", fake_launch)
    monkeypatch.setattr("backend.m0_api.worker.m3_launcher.is_alive", lambda pid, case_dir: False)

    def fake_postprocess(case_dir, run_dir, **kwargs):
        summary = Path(run_dir) / "summary"
        summary.mkdir(exist_ok=True)
        for name in ("max_depth", "max_velocity", "arrival_time"):
            (summary / f"{name}.tif").touch()
        return {"contract_version": "0.3.0", "run_id": kwargs["run_id"],
                "scenario_id": kwargs["scenario_id"], "model": "delft3d",
                "status": "postprocessed", "wall_time_s": 0.01}
    monkeypatch.setattr("backend.m3_common.postprocess.postprocess_dflowfm", fake_postprocess)
    # Feature 13: this test covers worker plumbing; acceptance itself is tested in
    # tests/m3_dflowfm/test_acceptance.py. Here the synthetic run is declared ACCEPTED.
    import backend.m3_dflowfm.acceptance as acceptance_module
    monkeypatch.setattr(acceptance_module, "evaluate_run", lambda *a, **k: {
        "status": "ACCEPTED" if accepted else "FAILED_ACCEPTANCE", "failed_checks": [] if accepted else ["poi_wetting"],
        "unavailable_checks": [], "checks": {}})

    worker = Worker()
    worker.tick()  # queued -> simulating
    worker.tick()  # first launch
    worker.tick()  # first failure queues one retry
    assert conn.execute("SELECT status FROM runs WHERE run_id = ?", (cases[0].run_id,)).fetchone()[0] == "queued"
    # Feature 17 (E5): the failed first attempt's .dia was archived intact before the retry.
    archived = data_dir / "synth/runs" / cases[0].run_id / "attempts/a00"
    assert (archived / "failed_output/model.dia").read_text().startswith("** ERROR")
    assert json.loads((archived / "attempt_meta.json").read_text())["attempt"] == 0
    worker.tick()  # second launch
    worker.tick()  # success, post-process, acceptance, M5 cache
    if not accepted:  # Feature 13: FAILED_ACCEPTANCE -> failed, no retry, no cache, outputs kept
        row = conn.execute("SELECT status, meta_json, error FROM runs WHERE run_id = ?", (cases[0].run_id,)).fetchone()
        assert row[0] == "failed" and row[2].startswith("FAILED_ACCEPTANCE") and attempts["count"] == 2
        assert json.loads(row[1])["acceptance"]["status"] == "FAILED_ACCEPTANCE"
        assert not (data_dir / "synth/emulator/delft3d/run_cache.json").exists()
        run_meta = json.loads((data_dir / "synth/runs" / cases[0].run_id / "run_meta.json").read_text())
        assert run_meta["acceptance"]["status"] == "FAILED_ACCEPTANCE" and run_meta["provenance"]["completed_at"]
        status = json.loads((data_dir / "synth/campaign_status.json").read_text())
        assert status["summary"]["FAILED_ACCEPTANCE"] == 1 and status["summary"]["SUCCEEDED"] == 0
        index = json.loads((data_dir / "synth/runs/index.json").read_text())
        assert index["runs"][0]["state"] == "FAILED_ACCEPTANCE" and index["runs"][0]["queryable"] is False
        worker.close()
        conn.close()
        return
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (cases[0].run_id,)).fetchone()
    assert row[0] == "postprocessed"
    run_meta = json.loads((data_dir / "synth/runs" / cases[0].run_id / "run_meta.json").read_text())
    assert run_meta["spinup_s"] == 7200.0 and run_meta["spinup_source"] == "case_meta.json"  # Feature 17 A5
    assert attempts["count"] == 2
    cache = json.loads((data_dir / "synth/emulator/delft3d/run_cache.json").read_text())
    assert cache["runs"][0]["params"] == design["scenarios"][0]["params"]
    status = json.loads((data_dir / "synth/campaign_status.json").read_text())
    assert status["done"] == 1 and status["running"] is None and status["failed"] == []
    worker.close()
    conn.close()

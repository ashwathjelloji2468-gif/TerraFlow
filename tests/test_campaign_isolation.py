"""Feature 13 phase 3: production-gated, isolated D-Flow FM campaigns (synthetic; no kernel)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

import backend.m3_dflowfm
from backend import campaign
from backend.m0_api import registry


@pytest.fixture
def synth_campaign(tmp_path, monkeypatch):
    data_dir, sites_dir = tmp_path / "data", tmp_path / "sites"
    monkeypatch.setenv("SIH26_DATA_DIR", str(data_dir))
    registry.init_db()
    raw = yaml.safe_load((Path(__file__).parent / "fixtures/shared/synth.yaml").read_text())
    raw["domains"]["far_field"]["inflow"]["base_flow"] = {"value": 1.0, "unit": "m^3/s", "source": "synthetic fixture",
                                                          "status": "placeholder"}
    sites_dir.mkdir()
    (sites_dir / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    (data_dir / "synth/design").mkdir(parents=True)
    scen = [{"scenario_id": f"synth__s00{i}", "kind": "design",
             "params": {"water_volume_m3": 1e6 * i, "breach_width_m": 10.0, "failure_time_s": 100.0}} for i in (1, 2, 3)]
    (data_dir / "synth/design/scenario_design.json").write_text(json.dumps({
        "contract_version": "0.3.0", "site_id": "synth", "model": "delft3d", "method": "maximin_lhs", "seed": 7,
        "n": 3, "inputs": [], "scenarios": scen, "extra": [], "has_placeholders": True, "caveats": [],
        "provenance": {"fingerprint": "f" * 64}}))
    gen = types.ModuleType("backend.m3_dflowfm.generator")
    gen.DEFAULT_STOP_S = 108000.0
    gen.placeholder_fields = lambda config: []

    def build_case(site_id, scenario_id, params, *, data_dir, sites_dir, case_dir, stop_s, demo=False):
        if scenario_id == "synth__s002":
            raise RuntimeError("synthetic generator failure for s002")
        Path(case_dir).mkdir(parents=True, exist_ok=True)
        return Path(case_dir), {"scenario_id": scenario_id}
    gen.build_case = build_case
    monkeypatch.setitem(sys.modules, "backend.m3_dflowfm.generator", gen)
    monkeypatch.setattr(backend.m3_dflowfm, "generator", gen, raising=False)
    hyd = __import__("backend.m2_breach.hydrograph", fromlist=["hydrograph"])
    monkeypatch.setattr(hyd, "hydrograph", lambda *a, **k: object())

    def write_hydrograph(hg, site_id, scenario_id, data_dir):
        p = Path(data_dir) / site_id / "breach/hydrographs" / f"{scenario_id}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("t,q\n")
        return p, p.with_suffix(".json")
    monkeypatch.setattr(hyd, "write_hydrograph", write_hydrograph)
    return data_dir, sites_dir


def test_production_campaign_is_blocked_for_placeholder_config(synth_campaign):
    data_dir, sites_dir = synth_campaign
    conn = registry.connect()
    job_id, results = campaign.run_dflowfm_campaign("synth", conn, data_dir, sites_dir, production=True)
    assert job_id is None and [r.status for r in results] == ["blocked"]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # nothing queued or built
    status = json.loads((data_dir / "synth/campaign_status.json").read_text())
    assert status["campaign_state"] == "BLOCKED" and "domains.far_field.inflow.base_flow" in status["production_gate"]["blocking"]
    conn.close()


def test_failed_scenario_isolated_and_successful_ones_preserved(synth_campaign):
    data_dir, sites_dir = synth_campaign
    conn = registry.connect()
    job_id, results = campaign.run_dflowfm_campaign("synth", conn, data_dir, sites_dir)
    by = {r.scenario_id: r.status for r in results}
    assert by == {"synth__s001": "queued", "synth__s002": "failed", "synth__s003": "queued"}
    # deterministic scenario -> run mapping and full provenance on every run
    for sid in ("synth__s001", "synth__s003"):
        prov = json.loads((data_dir / f"synth/runs/{sid}__delft3d/scenario.json").read_text())["provenance"]
        assert prov["run_id"] == f"{sid}__delft3d" and len(prov["input_fingerprint"]) == 64
        assert prov["code_version"] and prov["campaign_mode"] == "non_production" and prov["generated_at"]
    fp1 = json.loads((data_dir / "synth/runs/synth__s001__delft3d/scenario.json").read_text())["provenance"]["input_fingerprint"]
    # s001 finishes successfully; s003 fails acceptance (outputs kept for review).
    meta3 = {"acceptance": {"status": "FAILED_ACCEPTANCE", "failed_checks": ["poi_wetting"]}}
    with conn:
        conn.execute("UPDATE runs SET status='postprocessed' WHERE run_id='synth__s001__delft3d'")
        conn.execute("UPDATE runs SET status='failed', meta_json=? WHERE run_id='synth__s003__delft3d'", (json.dumps(meta3),))
    campaign.write_dflowfm_status(data_dir, "synth", conn, job_id)
    status = json.loads((data_dir / "synth/campaign_status.json").read_text())
    assert status["summary"] == {"QUEUED": 0, "RUNNING": 0, "SUCCEEDED": 1, "FAILED": 1, "FAILED_ACCEPTANCE": 1, "BLOCKED": 0}
    assert status["failed_acceptance"] == ["synth__s003__delft3d"]

    # Re-running the campaign never rebuilds/overwrites the succeeded or under-review runs.
    marker = data_dir / "synth/runs/synth__s001__delft3d/case/keep.txt"
    marker.write_text("result")
    _, again = campaign.run_dflowfm_campaign("synth", conn, data_dir, sites_dir)
    by2 = {r.scenario_id: r.status for r in again}
    assert by2["synth__s001"] == "preserved" and by2["synth__s003"] == "preserved"
    assert marker.read_text() == "result"
    assert conn.execute("SELECT status FROM runs WHERE run_id='synth__s001__delft3d'").fetchone()[0] == "postprocessed"
    fp_again = json.loads((data_dir / "synth/runs/synth__s001__delft3d/scenario.json").read_text())["provenance"]["input_fingerprint"]
    assert fp_again == fp1
    conn.close()


def test_run_state_mapping():
    assert campaign.run_state("failed", {"acceptance": {"status": "FAILED_ACCEPTANCE"}}) == "FAILED_ACCEPTANCE"
    assert campaign.run_state("failed", {}) == "FAILED"
    assert campaign.run_state("postprocessed", {}) == "SUCCEEDED"
    assert campaign.run_state("queued", {}) == "QUEUED" and campaign.run_state("running", {}) == "RUNNING"

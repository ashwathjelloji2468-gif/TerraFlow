"""Feature 6: GET /compare/{site}?scenario_id= reports the generic SPH/Delft3D pair statuses."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.m0_api.main import app

API = "/api/v1"
client = TestClient(app)


def test_compare_reports_pair_statuses_without_metrics(data_dir):
    runs = data_dir / "teesta" / "runs"
    (runs / "teesta__s001__delft3d").mkdir(parents=True)
    (runs / "teesta__s001__delft3d" / "run_meta.json").write_text(json.dumps({"status": "postprocessed"}))
    attempt = runs / "teesta__s001__sph" / "attempts" / "a00"
    attempt.mkdir(parents=True)
    (attempt / "execution.json").write_text(json.dumps({"success": False, "error": "DualSPHysics exited with status 1"}))

    r = client.get(f"{API}/compare/teesta", params={"scenario_id": "teesta__s001"})
    assert r.status_code == 200, r.text
    sv = r.json()["sph_vs_delft3d"]
    assert sv["available"] is False and sv["metrics"] == {}
    assert sv["status"]["solver_status"] == "FAILED"
    assert sv["status"]["comparison_status"] == "BLOCKED" and sv["status"]["validation_status"] == "BLOCKED"
    assert sv["run_ids"] == ["teesta__s001__delft3d", "teesta__s001__sph"]


def test_compare_serves_built_pair_sidecar(data_dir):
    out = data_dir / "teesta" / "compare" / "teesta__s002"
    out.mkdir(parents=True)
    sidecar = {"site_id": "teesta", "scenario_id": "teesta__s002",
               "sph_vs_delft3d": {"available": False, "domain": "nearfield", "time_window_s": 0.0, "metrics": {},
                                  "probes": [], "layers": [], "run_ids": ["teesta__s002__sph"],
                                  "status": {"solver_status": "SUCCEEDED", "postprocess_status": "SUCCEEDED",
                                             "comparison_status": "BLOCKED", "validation_status": "BLOCKED"}},
               "emulator_vs_physics": {"available": False, "held_out_run_id": None, "metrics": {}, "layers": []},
               "gp_vs_linear": {}, "when_to_use_key": "when_to_use_sph_delft3d", "caveats": []}
    (out / "compare.json").write_text(json.dumps(sidecar))
    r = client.get(f"{API}/compare/teesta", params={"scenario_id": "teesta__s002"})
    assert r.status_code == 200 and r.json()["sph_vs_delft3d"]["status"]["comparison_status"] == "BLOCKED"

"""Feature 13 phases 4-5: reproducible registration, queryability, run index, readiness endpoint,
and Feature 12 regressions. Synthetic run fixtures only (tests/m3_dflowfm/acceptance_fixtures.py)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.m0_api import registry, run_registration as rr, schemas
from backend.m0_api.main import app
from tests.m3_dflowfm.acceptance_fixtures import make_run

client = TestClient(app)
API = "/api/v1"


def _register(data_dir, **kw):
    return rr.register_existing_run("teesta", kw.pop("run_id"), data_dir=data_dir, model_stem="model", **kw)


def test_missing_run_meta_is_refused(data_dir):
    r = make_run(data_dir, "teesta", "s010", with_run_meta=False)
    with pytest.raises(rr.RegistrationRefused, match="run_meta.json missing"):
        _register(data_dir, run_id=r["run_id"])


def test_mismatched_identity_is_refused(data_dir):
    r = make_run(data_dir, "teesta", "s011", identity_run_id="s999__delft3d")
    with pytest.raises(rr.RegistrationRefused, match="identity"):
        _register(data_dir, run_id=r["run_id"])


def test_incomplete_outputs_refused(data_dir):
    r = make_run(data_dir, "teesta", "s012", missing_output="summary/max_velocity.tif")
    with pytest.raises(rr.RegistrationRefused, match="required outputs"):
        _register(data_dir, run_id=r["run_id"])


def test_solver_failure_refused_even_as_pilot(data_dir):
    r = make_run(data_dir, "teesta", "s013", dia_error=True)
    with pytest.raises(rr.RegistrationRefused, match="FAILED_ACCEPTANCE"):
        _register(data_dir, run_id=r["run_id"])
    with pytest.raises(rr.RegistrationRefused, match="solver_success"):
        _register(data_dir, run_id=r["run_id"], pilot=True)


def test_poi_smoke_failure_refused_and_not_queryable(data_dir):
    r = make_run(data_dir, "teesta", "s014", poi_dry=True)
    with pytest.raises(rr.RegistrationRefused, match="poi_wetting"):
        _register(data_dir, run_id=r["run_id"])
    body = {"site_id": "teesta", "scenario_id": "s014", "model": "delft3d", "mode": "scenario", "inputs": {}}
    resp = client.post(f"{API}/flood/query", json=body)
    assert resp.status_code == 404 and resp.json()["detail"]["error"]["code"] == "real_run_not_found"


def test_valid_run_registers_and_is_queryable(data_dir):
    r = make_run(data_dir, "teesta", "s015")
    out = _register(data_dir, run_id=r["run_id"])
    assert out["acceptance_status"] == "ACCEPTED" and out["run_class"] == "accepted"
    meta = json.loads((r["run_dir"] / "run_meta.json").read_text())
    assert meta["acceptance"]["status"] == "ACCEPTED" and (r["run_dir"] / "run_meta.original.json").is_file()
    assert json.loads((r["run_dir"] / "acceptance.json").read_text())["status"] == "ACCEPTED"
    index = json.loads((data_dir / "teesta" / "runs" / "index.json").read_text())
    assert index["runs"][0]["queryable"] is True and index["runs"][0]["state"] == "SUCCEEDED"
    cache = json.loads((data_dir / "teesta" / "emulator" / "delft3d" / "run_cache.json").read_text())
    assert any(entry["run_id"] == r["run_id"] for entry in cache["runs"])


def test_pilot_run_keeps_failed_acceptance_and_pilot_caveat(data_dir):
    r = make_run(data_dir, "teesta", "s016", poi_dry=True)
    out = _register(data_dir, run_id=r["run_id"], pilot=True)
    assert out["run_class"] == "pilot" and out["acceptance_status"] == "FAILED_ACCEPTANCE"
    assert not (data_dir / "teesta" / "emulator" / "delft3d" / "run_cache.json").exists()  # never training data
    readiness = client.get(f"{API}/sites/teesta/readiness").json()
    schemas.validate("site_readiness.schema.json", readiness)
    run = next(x for x in readiness["runs"] if x["run_id"] == r["run_id"])
    assert run["status"] == "PILOT" and run["acceptance_status"] == "FAILED_ACCEPTANCE"
    assert run["failed_checks"] == ["poi_wetting"]
    assert any(c["id"] == "pilot_run_not_production_accepted" for c in readiness["caveats"])


def test_legacy_unevaluated_run_is_not_queryable(data_dir):
    """A row registered before acceptance existed (e.g. the hand-made MVP row) is not served."""
    r = make_run(data_dir, "teesta", "s017")
    with registry.connect() as conn:
        conn.execute("INSERT INTO scenarios VALUES (?,?,?,?,?)", ("s017", "teesta", "design", "{}", registry.utc_now()))
        conn.execute("INSERT INTO runs (run_id,scenario_id,model,status,run_dir) VALUES (?,?,?,?,?)",
                     (r["run_id"], "s017", "delft3d", "postprocessed", str(r["run_dir"])))
    body = {"site_id": "teesta", "scenario_id": "s017", "model": "delft3d", "mode": "scenario", "inputs": {}}
    resp = client.post(f"{API}/flood/query", json=body)
    assert resp.status_code == 404 and "not accepted" in resp.json()["detail"]["error"]["message"]
    run = next(x for x in client.get(f"{API}/sites/teesta/readiness").json()["runs"] if x["run_id"] == r["run_id"])
    assert run["status"] == "NOT_EVALUATED" and run["queryable"] is False


def test_readiness_teesta_blocked_and_never_empty_success(data_dir):
    body = client.get(f"{API}/sites/teesta/readiness").json()
    schemas.validate("site_readiness.schema.json", body)
    assert body["production_verdict"] == "BLOCKED" and body["blocking"]
    assert body["runs"] == [] and all(v == 0 for v in body["run_counts"].values())
    assert any(c["id"] == "production_gate_blocked" for c in body["caveats"])
    statuses = {i["status"] for i in body["inputs"]}
    assert {"READY", "PLACEHOLDER", "BLOCKED", "CONTROLLED_PASS"} <= statuses
    assert client.get(f"{API}/sites/nosuch/readiness").status_code == 404


def test_feature12_teesta_mvp_query_still_honest_404(data_dir):
    body = {"site_id": "teesta", "scenario_id": "teesta_2023_mvp", "model": "delft3d", "mode": "scenario", "inputs": {}}
    resp = client.post(f"{API}/flood/query", json=body)
    assert resp.status_code == 404 and resp.json()["detail"]["error"]["code"] == "real_run_not_found"

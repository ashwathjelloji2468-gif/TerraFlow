"""Feature 3 through the real job system: the onboarding `breach` stage writes breach_params.json
(with Feature 2 evidence) and the reference hydrographs, and logs one event per dam/case."""

from __future__ import annotations

import json

import yaml
from fastapi.testclient import TestClient

from backend.m0_api import schemas
from backend.m0_api.main import app
from backend.m0_api.worker import Worker
from tests.m0_api.conftest import wait_until
from tests.m0_api.test_synthetic_onboarding_pipeline import SITE_PATH, _seed_synthetic_raw

client = TestClient(app)


def test_breach_stage_writes_params_reference_hydrographs_and_events(data_dir):
    site_raw = yaml.safe_load(SITE_PATH.read_text(encoding="utf-8"))
    site_id = site_raw["site"]["id"]
    _seed_synthetic_raw(data_dir, site_raw)
    job_id = client.post("/api/v1/sites", json={"site_config": site_raw, "demo_mode": True}).json()["job_id"]
    worker = Worker()
    worker.acquire_lock()
    try:
        wait_until(lambda: client.get(f"/api/v1/jobs/{job_id}").json()["stage"] in {"design", "simulating", "failed"},
                   worker.tick, timeout_s=60)
    finally:
        worker.close()
    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["stage"] != "failed", status["error"]

    payload = json.loads((data_dir / site_id / "breach" / "breach_params.json").read_text())
    schemas.validate("breach_params.schema.json", payload)
    assert payload["provenance"]["evidence"]["dem"]["status"] == "available"  # Feature 2 terrain
    records = payload["provenance"]["reference_hydrographs"]
    assert {r["case"] for r in records} == {"low", "high"}
    for rec in records:
        if rec["status"] == "written":
            assert (data_dir / site_id / rec["csv"]).is_file()
        else:
            assert rec["reason"]
    log = "\n".join(status["log_tail"])
    dam_id = site_raw["dams"][0]["id"]
    assert f"breach {dam_id}:" in log
    assert f"breach {dam_id} low:" in log and f"breach {dam_id} high:" in log

"""Feature 16 P7: the existing GET /sites/{id}/readiness (contract unchanged) serves a synthetic
non-Teesta site site-neutrally, and Teesta's readiness is unchanged (BLOCKED)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from backend.m0_api import schemas
from backend.m0_api.main import app

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "f16" / "synth_river_alpha.yaml"
SID = "synth_river_alpha"
client = TestClient(app)
SITE_WORDS = ("teesta", "lhonak", "chungthang", "south_lhonak", "src_043", "src_073", "32645")


def _register():
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    r = client.post("/api/v1/sites", json={"site_config": raw, "demo_mode": True})
    assert r.status_code == 202, r.text


def test_synthetic_site_readiness_is_site_neutral(data_dir):
    _register()
    r = client.get(f"/api/v1/sites/{SID}/readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    schemas.validate("site_readiness.schema.json", body)
    assert body["site_id"] == SID and body["config_loaded"] is True
    assert body["production_verdict"] == "BLOCKED"  # no terrain artifacts yet: never READY
    by = {i["key"]: i for i in body["inputs"]}
    assert by["terrain_artifacts"]["status"] == "BLOCKED"
    cfg_items = [i for i in body["inputs"] if i["category"] == "site_config"]
    assert cfg_items and {i["status"] for i in cfg_items} == {"READY"}  # fully sourced synthetic fixture
    assert {i["status"] for i in body["inputs"]} <= {"READY", "PLACEHOLDER", "BLOCKED", "CONTROLLED_PASS"}
    # Site-specific content (keys, sources, site config details) carries no Teesta text. Shared team gates
    # (docs/real_input_checklist.json notes) are global documentation, quoted verbatim for every site.
    site_part = [i for i in body["inputs"] if i["category"] != "shared_gate"]
    text = json.dumps({**body, "inputs": site_part, "blocking": [b for b in body["blocking"] if b in {i["key"] for i in site_part}],
                       "evaluated_at": None}).lower()
    for w in SITE_WORDS:
        assert w not in text, w
    assert all(w not in i["key"].lower() for i in body["inputs"] for w in SITE_WORDS)


def test_readiness_does_not_trigger_acquisition(data_dir, monkeypatch):
    _register()
    import backend.m1_terrain.download as dl
    import backend.m1_terrain.discharge as dis
    monkeypatch.setattr(dl, "fetch_opentopography", lambda *a, **k: (_ for _ in ()).throw(AssertionError("download")))
    monkeypatch.setattr(dis, "fetch_discharge", lambda *a, **k: (_ for _ in ()).throw(AssertionError("glofas")))
    before = sorted(p.relative_to(data_dir) for p in (data_dir / SID).rglob("*"))
    assert client.get(f"/api/v1/sites/{SID}/readiness").status_code == 200
    assert sorted(p.relative_to(data_dir) for p in (data_dir / SID).rglob("*")) == before  # read-only


def test_teesta_readiness_unchanged(data_dir):
    before = (REPO / "sites" / "teesta.yaml").read_bytes()
    body = client.get("/api/v1/sites/teesta/readiness").json()
    schemas.validate("site_readiness.schema.json", body)
    assert body["production_verdict"] == "BLOCKED" and body["counts"]["PLACEHOLDER"] > 0
    assert "domains.far_field.inflow.base_flow" in body["blocking"]
    assert (REPO / "sites" / "teesta.yaml").read_bytes() == before


def test_unsafe_site_id_rejected(data_dir):
    for bad in ("..%2Fteesta", "Teesta", "a"):
        assert client.get(f"/api/v1/sites/{bad}/readiness").status_code in (404, 422)

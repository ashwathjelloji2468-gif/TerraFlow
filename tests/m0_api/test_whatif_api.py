"""Feature 4 API: GET /sites/{id}/design and POST /sites/{id}/whatif (real M2 + real hydrograph)."""

from __future__ import annotations

import copy
import importlib
import json
import warnings
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from backend.m0_api import schemas, site_registry
from backend.m0_api.main import app
from backend.m2_breach.reference import run_breach_stage
from backend.m5_emulator import scenario_design as sd
from tests.shared.conftest import SYNTH_PATH, fully_sourced

API = "/api/v1"
client = TestClient(app)
SETTINGS = sd.ScenarioDesignSettings(n=6, n_holdout=1, seed=5)


def _register(site_id: str = "whatif_valley") -> dict:
    raw = fully_sourced(copy.deepcopy(yaml.safe_load(SYNTH_PATH.read_text())))
    raw["site"].update({"id": site_id, "name": f"What-if valley {site_id}"})
    assert client.post(f"{API}/sites", json={"site_config": raw}).status_code == 202
    return raw


def _design(data_dir: Path, site_id: str = "whatif_valley") -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = site_registry.load(site_id)
    run_breach_stage(cfg, data_dir=data_dir)
    sd.write_scenario_design(cfg, "synth_lake", data_dir, SETTINGS)


def test_get_design_404_before_the_design_stage():
    _register()
    r = client.get(f"{API}/sites/whatif_valley/design")
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "design_not_found"
    assert client.get(f"{API}/sites/nosuchsite/design").status_code == 404


def test_get_design_returns_the_persisted_design(data_dir):
    _register()
    _design(data_dir)
    r = client.get(f"{API}/sites/whatif_valley/design")
    assert r.status_code == 200
    body = r.json()
    schemas.validate("scenario_design.schema.json", body)
    assert body["scenarios"][0]["scenario_id"] == "whatif_valley__s001"
    assert body["provenance"]["hydrograph_method"] == "triangular"
    assert "peak_discharge_m3s" in body["scenarios"][0]["params"]


def test_whatif_runs_real_m2_and_a_real_hydrograph(data_dir):
    _register()
    r = client.post(f"{API}/sites/whatif_valley/whatif",
                    json={"inputs": {"water_volume_m3": 2.0e6}, "scenario": {"breach_width_m": 60.0}, "case": "low"})
    assert r.status_code == 200, r.text
    body = r.json()
    schemas.validate("whatif_response.schema.json", body)
    assert body["status"] == "ok" and body["hydrograph_method"] == "triangular"
    assert body["breach_params"]["inputs_used"]["Vw_m3"] == 2.0e6  # M2 recomputed with the override
    assert body["scenario_params"]["breach_width_m"] == 60.0
    assert body["param_basis"]["failure_time_s"].startswith("M2 range high")
    qp = body["breach_params"]["parameters"]["peak_discharge_m3s"]
    assert body["scenario_params"]["peak_discharge_m3s"] == qp["low"]
    hg = body["hydrograph"]
    assert hg["peak_q_m3s"] == qp["low"] and len(hg["t_s"]) == len(hg["q_m3s"]) > 2
    assert abs(hg["volume_m3"] - 2.0e6) / 2.0e6 < 1e-6
    assert body["has_placeholders"] is True  # overrides are unverified
    assert body["saved_scenario_id"] is None


def test_invalid_overrides_are_422():
    _register()
    for bad in ({"inputs": {"water_volume_m3": -5}}, {"inputs": {"lake": 1}}, {"case": "medium"},
                {"dam_id": "no_such_dam"}, {"save_as": "Bad Name"}):
        r = client.post(f"{API}/sites/whatif_valley/whatif", json=bad)
        assert r.status_code == 422, (bad, r.text)


def test_blocked_override_returns_explicit_blocked_result():
    _register()
    r = client.post(f"{API}/sites/whatif_valley/whatif", json={"inputs": {"dam_type": "FD"}})
    assert r.status_code == 200
    body = r.json()
    schemas.validate("whatif_response.schema.json", body)
    assert body["status"] == "blocked" and body["hydrograph"] is None
    assert any("Z20" in reason for reason in body["blocked_reasons"])
    assert "peak_discharge_m3s" not in body["scenario_params"]  # nothing invented
    r = client.post(f"{API}/sites/whatif_valley/whatif", json={"inputs": {"dam_type": "FD"}, "save_as": "fd_case"})
    assert r.status_code == 422 and "blocked" in r.json()["detail"]["error"]["message"]


def test_saved_whatif_persists_joins_the_design_and_survives_restart(data_dir):
    _register()
    _design(data_dir)
    r = client.post(f"{API}/sites/whatif_valley/whatif",
                    json={"scenario": {"breach_width_m": 55.0}, "save_as": "narrow_breach"})
    assert r.status_code == 200, r.text
    assert r.json()["saved_scenario_id"] == "whatif_valley__n_narrow_breach"
    named = json.loads((data_dir / "whatif_valley" / "design" / "named_scenarios.json").read_text())
    assert named["scenarios"][0]["params"]["breach_width_m"] == 55.0

    import backend.m0_api.main as main_module

    restarted = TestClient(importlib.reload(main_module).app)
    design = restarted.get(f"{API}/sites/whatif_valley/design").json()
    saved = next(e for e in design["extra"] if e["scenario_id"] == "whatif_valley__n_narrow_breach")
    assert saved["kind"] == "named" and saved["params"]["breach_width_m"] == 55.0

    # a changed design (new seed) keeps the saved named scenario in extra
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = site_registry.load("whatif_valley")
    sd.write_scenario_design(cfg, "synth_lake", data_dir, sd.ScenarioDesignSettings(n=6, n_holdout=1, seed=6))
    design = restarted.get(f"{API}/sites/whatif_valley/design").json()
    assert "whatif_valley__n_narrow_breach" in {e["scenario_id"] for e in design["extra"]}


def test_duplicate_saved_name_is_409():
    _register()
    body = {"scenario": {"breach_width_m": 55.0}, "save_as": "twice"}
    assert client.post(f"{API}/sites/whatif_valley/whatif", json=body).status_code == 200
    r = client.post(f"{API}/sites/whatif_valley/whatif", json=body)
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "scenario_name_taken"


def test_triangular_whatif_with_input_override_is_saved_reproducibly():
    """Triangular: the saved (V, B_ave, T_f, Q_p) alone reproduce the hydrograph, so a breach-input
    override (dam height -> new M2 Q_p range) can be saved."""
    _register()
    r = client.post(f"{API}/sites/whatif_valley/whatif", json={"inputs": {"dam_height_m": 40.0}, "save_as": "taller_dam"})
    assert r.status_code == 200, r.text
    assert r.json()["saved_scenario_id"] == "whatif_valley__n_taller_dam"


def test_weir_whatif_that_scenario_params_cannot_reproduce_is_refused():
    """Weir: breach height drives the hydrograph but is not a scenario parameter, so saving it
    would make Feature 5 run a different flood than the one shown -- refused with 422."""
    from tests.m2_breach.conftest import SYNTH_BREACH_HYDROGRAPH, SYNTH_VOLUME_ELEVATION_RELATION

    raw = fully_sourced(copy.deepcopy(yaml.safe_load(SYNTH_PATH.read_text())))
    raw["site"].update({"id": "weir_valley", "name": "Weir valley"})
    raw["dams"][0].update(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION, breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    assert client.post(f"{API}/sites", json={"site_config": raw}).status_code == 202
    ok = client.post(f"{API}/sites/weir_valley/whatif", json={"scenario": {"breach_width_m": 50.0}})
    assert ok.status_code == 200 and ok.json()["hydrograph_method"] == "breach_growth_weir"
    assert "peak_discharge_m3s" not in ok.json()["scenario_params"]
    r = client.post(f"{API}/sites/weir_valley/whatif",
                    json={"inputs": {"breach_height_m": 3.0}, "scenario": {"breach_width_m": 50.0}, "save_as": "low_breach"})
    assert r.status_code == 422 and "cannot be saved" in r.json()["detail"]["error"]["message"]

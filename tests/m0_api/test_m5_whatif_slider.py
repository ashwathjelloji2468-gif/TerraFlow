"""Feature 8: instant what-if — slider inputs mapped to exact M5 inputs, predicted by the persisted
emulator. Uses the same in-test synthetic emulator as Feature 7 (`test_m5_query.py`): a test
fixture saved into the temporary data dir, not a trained Teesta model."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from backend.m0_api import schemas
from backend.m5_emulator import monte_carlo as mc
from backend.m5_emulator import sliders
from backend.m5_emulator.emulator import FloodEmulator
from backend.m5_emulator.inputs import InputSpec
from tests.m0_api.test_m5_query import API, SITE, _registry_row, client, synthetic_emulator, trained_site  # noqa: F401

VOL = InputSpec("water_volume_m3", "log10", 1.0e5, 1.0e7)
WIDTH = InputSpec("breach_width_m", "linear", 20.0, 120.0)


# --- mapping maths (contract §3.3) --------------------------------------------------------------
def test_linear_mapping_endpoints_and_midpoint():
    assert sliders.slider_to_value(0, 20.0, 120.0, "linear") == 20.0
    assert sliders.slider_to_value(10, 20.0, 120.0, "linear") == 120.0
    assert sliders.slider_to_value(5, 20.0, 120.0, "linear") == 70.0
    assert sliders.slider_to_value(3, 20.0, 120.0, "linear") == pytest.approx(20.0 + 0.3 * 100.0)


def test_log_mapping_is_linear_in_ln_value():
    assert sliders.slider_to_value(0, 1e5, 1e7, "log") == pytest.approx(1e5)
    assert sliders.slider_to_value(10, 1e5, 1e7, "log") == pytest.approx(1e7)
    assert sliders.slider_to_value(5, 1e5, 1e7, "log") == pytest.approx(math.sqrt(1e5 * 1e7))  # geometric mean
    p = 2.5
    assert math.log(sliders.slider_to_value(p, 1e5, 1e7, "log")) == pytest.approx(
        math.log(1e5) + p / 10 * (math.log(1e7) - math.log(1e5)))


def test_mapping_is_deterministic_and_follows_emulator_scaling():
    a, b = sliders.map_slider(VOL, 4.2), sliders.map_slider(VOL, 4.2)
    assert a == b and a["mapping"] == "log" and "log10" in a["mapping_basis"]
    assert sliders.map_slider(WIDTH, 4.2)["mapping"] == "linear"
    assert sliders.map_slider(WIDTH, 4.2, "log")["mapping_basis"].startswith("site config")


@pytest.mark.parametrize("position", [-0.01, 10.01, float("nan"), float("inf")])
def test_out_of_range_positions_rejected(position):
    with pytest.raises(sliders.SliderError):
        sliders.slider_to_value(position, 20.0, 120.0, "linear")


def test_mapped_values_never_leave_the_training_box():
    for p in np.linspace(0, 10, 101):
        v = sliders.map_slider(VOL, float(p))["value"]
        assert VOL.low <= v <= VOL.high


# --- API ----------------------------------------------------------------------------------------
def _sliders(emulator: FloodEmulator, position: float = 3.0) -> dict:
    return {s.name: {"type": "slider", "position": position} for s in emulator.input_scaler.specs}


@pytest.fixture
def no_solvers(monkeypatch):
    """Any solver launch or training during a what-if query fails the test."""
    def forbidden(*args, **kwargs):
        raise AssertionError("a what-if query must not start a solver or train an emulator")

    monkeypatch.setattr("backend.m3_dflowfm.launcher.launch_case", forbidden)
    monkeypatch.setattr("backend.m4_sph.launcher.launch_case", forbidden)
    monkeypatch.setattr("backend.m4_sph.launcher.run_case_sync", forbidden)
    monkeypatch.setattr(FloodEmulator, "fit", classmethod(lambda cls, *a, **k: forbidden()))


def test_slider_scenario_query_is_predicted_by_m5(data_dir, trained_site, synthetic_emulator, no_solvers):
    body = {"site_id": SITE, "model": "delft3d", "mode": "scenario", "inputs": _sliders(synthetic_emulator, 3.0)}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    schemas.validate("flood_query_response.schema.json", payload)
    assert payload["method"] == "gp_emulator" and payload["mode"] == "scenario"

    p = payload["provenance"]["parameters"]
    assert p["query_type"] == "slider" and p["slider_mapping_version"] == sliders.MAPPING_VERSION
    assert p["solvers_started"] == [] and p["empirical_fallback_used"] is False
    for spec in synthetic_emulator.input_scaler.specs:
        expected = sliders.slider_to_value(3.0, spec.low, spec.high, sliders.default_mapping(spec))
        rec = p["slider_inputs"][spec.name]
        assert rec["position"] == 3.0 and rec["value"] == pytest.approx(expected)
        assert p["mapped_exact_inputs"][spec.name] == pytest.approx(expected)
        resolved = payload["resolved_inputs"][spec.name]
        assert resolved["value"] == pytest.approx(expected) and resolved["kind"] == "input"
        assert resolved["basis"].startswith("slider position 3")

    # Uncertainty (existing GP analytic P10/P90) and confidence (existing confidence.py).
    depth = payload["summary"]["max_depth_m"]
    assert depth["interval"] == "P10-P90" and depth["low"] <= depth["value"] <= depth["high"]
    assert p["uncertainty_method"].startswith("gp_analytic")
    assert set(payload["confidence"]) >= {"overall", "extent", "depth", "arrival", "velocity"}
    assert p["confidence_basis"].startswith("confidence.combine") and "UNKNOWN" in p["confidence_basis"]
    assert any(c["id"] == "emulator_not_validated" for c in payload["caveats"])

    qdir = data_dir / SITE / "queries" / payload["query_id"]
    assert json.loads((qdir / "result.json").read_text())["provenance"]["parameters"]["query_type"] == "slider"
    for layer in payload["layers"]:
        assert (qdir / "layers" / f"{layer['layer_id']}.tif").is_file()
    assert (qdir / "extent.geojson").is_file()
    assert _registry_row(payload["query_id"])["status"] == "complete"


def test_slider_equals_equivalent_exact_query(data_dir, trained_site, synthetic_emulator):
    """Same prediction whether the user slides to p or types the mapped exact values."""
    slider = client.post(f"{API}/flood/query", json={"site_id": SITE, "model": "delft3d", "mode": "scenario",
                                                     "inputs": _sliders(synthetic_emulator, 7.0)}).json()
    exact_inputs = {k: {"type": "exact", "value": v}
                    for k, v in slider["provenance"]["parameters"]["mapped_exact_inputs"].items()}
    exact = client.post(f"{API}/flood/query", json={"site_id": SITE, "model": "delft3d", "mode": "scenario",
                                                    "inputs": exact_inputs}).json()
    assert exact["provenance"]["parameters"]["query_type"] == "exact"
    for key in ("inundated_area_m2", "max_depth_m", "max_velocity_ms"):
        assert slider["summary"][key]["value"] == pytest.approx(exact["summary"][key]["value"])
    assert exact["provenance"]["parameters"]["emulator_cache_hit"] is True  # second call reuses the load


def test_unknown_breach_slider_uses_monte_carlo(data_dir, trained_site, synthetic_emulator, monkeypatch):
    calls = []
    real = mc.run_monte_carlo
    monkeypatch.setattr(mc, "run_monte_carlo", lambda *a, **k: calls.append(k) or real(*a, **k))
    first = synthetic_emulator.input_scaler.specs[0].name
    body = {"site_id": SITE, "model": "delft3d", "mode": "unknown_breach",
            "inputs": {first: {"type": "slider", "position": 6}}, "options": {"n_samples": 100, "seed": 11}}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 200, r.text
    p = r.json()["provenance"]["parameters"]
    assert calls and p["n_samples"] == 100 and p["seed"] == 11
    assert p["uncertainty_method"].startswith("monte_carlo") and list(p["slider_inputs"]) == [first]


@pytest.mark.parametrize("position", [-1, 10.5])
def test_out_of_range_slider_is_422(data_dir, trained_site, synthetic_emulator, position):
    inputs = _sliders(synthetic_emulator)
    inputs[next(iter(inputs))] = {"type": "slider", "position": position}
    r = client.post(f"{API}/flood/query", json={"site_id": SITE, "model": "delft3d", "mode": "scenario", "inputs": inputs})
    assert r.status_code == 422 and r.json()["detail"]["error"]["code"] == "invalid_request"


def test_slider_for_unknown_input_is_422(data_dir, trained_site, synthetic_emulator):
    inputs = {**_sliders(synthetic_emulator), "manning_multiplier": {"type": "slider", "position": 5}}
    r = client.post(f"{API}/flood/query", json={"site_id": SITE, "model": "delft3d", "mode": "scenario", "inputs": inputs})
    assert r.status_code == 422 and r.json()["detail"]["error"]["code"] == "invalid_emulator_inputs"
    assert "manning_multiplier" in r.json()["detail"]["error"]["message"]


def test_slider_without_emulator_never_predicts(data_dir):
    body = {"site_id": SITE, "model": "delft3d", "mode": "scenario",
            "inputs": {"breach_width_m": {"type": "slider", "position": 5}}}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err["details"]["emulator"]["available"] is False
    assert not (data_dir / SITE / "queries").exists() and not (data_dir / SITE / "emulator").exists()


def test_cache_reloads_a_retrained_emulator(data_dir, trained_site):
    from backend.m5_emulator import service

    first, hit1 = service.load_emulator(trained_site)
    again, hit2 = service.load_emulator(trained_site)
    assert again is first and hit2 is True
    manifest = trained_site / "manifest.json"
    manifest.write_text(manifest.read_text() + "\n")  # any change to a persisted file invalidates
    reloaded, hit3 = service.load_emulator(trained_site)
    assert hit3 is False and reloaded is not first

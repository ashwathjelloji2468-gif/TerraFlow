"""Feature 7: POST /flood/query through a persisted M5 emulator.

The emulator here is fitted in-test on M5's own synthetic library (`library.build_synthetic_library`,
docs/m5_specs.md §7) and saved with `FloodEmulator.save` into the test's temporary data dir. It is a
test emulator, not a trained Teesta model; it exercises the real save/load/get_flood code path."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from backend.m0_api import registry, schemas
from backend.m0_api.main import app
from backend.m5_emulator import library as lib
from backend.m5_emulator import monte_carlo as mc
from backend.m5_emulator import service
from backend.m5_emulator import synthetic as sw
from backend.m5_emulator.emulator import EmulatorSettings, FloodEmulator
from backend.m5_emulator.inputs import make_input_specs

API = "/api/v1"
SITE = "teesta"  # a configured site id; the emulator itself is synthetic (see module docstring)
client = TestClient(app)


@pytest.fixture(scope="module")
def synthetic_emulator() -> FloodEmulator:
    library = lib.build_synthetic_library(sw.small_grid(SITE), n=12, seed=7)
    ranges = {name: (float(library.X_raw[:, i].min()), float(library.X_raw[:, i].max()))
              for i, name in enumerate(lib.INPUT_ORDER)}
    return FloodEmulator.fit(
        site_id=SITE, model="delft3d", X_raw=library.X_raw,
        maps={"max_depth": library.max_depth, "max_velocity": library.max_velocity, "arrival_time": library.arrival_time},
        grid=library.grid, input_specs=make_input_specs(ranges), run_ids=library.run_ids,
        t_end_s=library.t_end_s, settings=EmulatorSettings(seed=7, n_restarts=1),
    )


@pytest.fixture
def trained_site(data_dir, synthetic_emulator) -> Path:
    out = synthetic_emulator.save(data_dir / SITE / "emulator" / "delft3d")
    grid = synthetic_emulator.grid
    rows = []
    for name, chainage in sw.SYNTHETIC_POIS.items():
        row, col = divmod(sw.poi_cell_index(grid, chainage), grid.width)
        rows.append({"poi_id": f"{SITE}__poi__{name}", "name": name, "kind": "town",
                     "geometry": Point(grid.origin_x + (col + 0.5) * grid.cell_size_m,
                                       grid.origin_y - (row + 0.5) * grid.cell_size_m)})
    (data_dir / SITE / "terrain").mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(rows, crs=f"EPSG:{grid.crs_epsg}").to_file(data_dir / SITE / "terrain" / "pois.gpkg")
    return out


def _exact(emulator: FloodEmulator) -> dict:
    return {s.name: {"type": "exact", "value": (s.low + s.high) / 2} for s in emulator.input_scaler.specs}


def _registry_row(query_id: str):
    conn = registry.connect()
    try:
        return conn.execute("SELECT status, result_path FROM queries WHERE query_id=?", (query_id,)).fetchone()
    finally:
        conn.close()


def test_emulator_detected_and_incomplete_emulator_rejected(data_dir, trained_site):
    status = service.emulator_status(data_dir, SITE, "delft3d")
    assert status["available"] is True and status["manifest"].endswith("manifest.json")
    assert service.emulator_status(data_dir, SITE, "sph")["available"] is False  # no SPH emulator invented
    (trained_site / "gp_depth.joblib").unlink()
    broken = service.emulator_status(data_dir, SITE, "delft3d")
    assert broken["available"] is False and "gp_depth.joblib" in broken["missing"]


def test_scenario_query_runs_m5_and_writes_artifacts(data_dir, trained_site, synthetic_emulator):
    body = {"site_id": SITE, "model": "delft3d", "mode": "scenario", "inputs": _exact(synthetic_emulator)}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    schemas.validate("flood_query_response.schema.json", payload)
    assert payload["method"] == "gp_emulator" and payload["mode"] == "scenario"
    qdir = data_dir / SITE / "queries" / payload["query_id"]
    stored = json.loads((qdir / "result.json").read_text())
    assert stored["query_id"] == payload["query_id"]
    ids = {layer["layer_id"] for layer in payload["layers"]}
    assert {"depth_p50", "depth_p10", "depth_p90", "p_inundation", "extent_class", "arrival_p50", "velocity_p90"} <= ids
    assert "velocity_p10" not in ids  # M5 array with no contract layer id: not published
    for layer in payload["layers"]:  # every URL points at a written file, and renders
        assert (qdir / "layers" / f"{layer['layer_id']}.tif").is_file()
        png = client.get(layer["url"])
        assert png.status_code == 200 and png.headers["content-type"] == "image/png", (layer["layer_id"], png.text)
    assert client.get(payload["vectors"]["extent_url"]).status_code == 200
    row = _registry_row(payload["query_id"])
    assert row["status"] == "complete" and row["result_path"] == str(qdir / "result.json")
    assert client.get(f"{API}/flood/{payload['query_id']}").json()["query_id"] == payload["query_id"]

    prov = payload["provenance"]
    manifest = json.loads((trained_site / "manifest.json").read_text())
    assert prov["method"] == "gp_emulator" and prov["emulator_manifest"] == str(trained_site / "manifest.json")
    assert prov["run_ids"] == manifest["run_ids"]
    p = prov["parameters"]
    assert p["site_id"] == SITE and p["model"] == "delft3d" and p["query_id"] == payload["query_id"]
    assert p["trained_at"] == manifest["trained_at"] and p["empirical_fallback_used"] is False
    assert p["validation_skill"] is None  # no loocv.json -> no skill claimed
    assert any(c["id"] == "emulator_not_validated" for c in payload["caveats"])
    assert set(p["pois_used"]) == set(sw.SYNTHETIC_POIS)


def test_unknown_breach_uses_existing_monte_carlo(data_dir, trained_site, synthetic_emulator, monkeypatch):
    calls = []
    real = mc.run_monte_carlo

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mc, "run_monte_carlo", spy)
    name = synthetic_emulator.input_scaler.specs[0].name
    body = {"site_id": SITE, "model": "delft3d", "mode": "unknown_breach",
            "inputs": {name: _exact(synthetic_emulator)[name]}, "options": {"n_samples": 120, "seed": 3}}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 200, r.text
    assert calls, "unknown_breach must go through monte_carlo.run_monte_carlo"
    payload = r.json()
    assert payload["mode"] == "unknown_breach" and payload["provenance"]["parameters"]["n_samples"] == 120
    assert payload["provenance"]["parameters"]["seed"] == 3


def test_missing_emulator_never_predicts(data_dir):
    body = {"site_id": SITE, "model": "delft3d", "mode": "scenario",
            "inputs": {"breach_width_m": {"type": "exact", "value": 50.0}}}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err["code"] == "scenario_id_required"
    assert err["details"]["emulator"]["available"] is False
    fb = err["details"]["empirical_fallback"]
    assert fb["available"] is False and "hand.tif" in fb["missing"]
    assert not (data_dir / SITE / "queries").exists()


def test_fallback_blocked_even_with_artifacts_present(data_dir):
    terrain = data_dir / SITE / "terrain"
    terrain.mkdir(parents=True)
    for name in service.FALLBACK_REQUIRED:
        (terrain / name).write_text("")
    fb = service.fallback_status(data_dir, SITE)
    assert fb["available"] is False and fb["missing"] == [] and "loader" in fb["reason"]


def test_slider_inputs_are_rejected_not_mapped(data_dir, trained_site, synthetic_emulator):
    inputs = _exact(synthetic_emulator)
    first = next(iter(inputs))
    inputs[first] = {"type": "slider", "position": 5}
    r = client.post(f"{API}/flood/query", json={"site_id": SITE, "model": "delft3d", "mode": "scenario", "inputs": inputs})
    assert r.status_code == 422 and r.json()["detail"]["error"]["code"] == "invalid_emulator_inputs"
    query_id = r.json()["detail"]["error"]["details"]["query_id"]
    assert _registry_row(query_id)["status"] == "failed"


def test_registered_scenario_still_uses_direct_path(data_dir, trained_site, synthetic_emulator, monkeypatch):
    from backend.m0_api import real_query

    seen = []

    def direct(site_id, scenario_id, model, query_id, body):
        seen.append(scenario_id)
        raise FileNotFoundError(f"no registered run {scenario_id}")

    monkeypatch.setattr(real_query, "resolve_registered_run", direct)
    body = {"site_id": SITE, "scenario_id": "teesta__s001", "model": "delft3d", "mode": "scenario",
            "inputs": _exact(synthetic_emulator)}
    r = client.post(f"{API}/flood/query", json=body)
    assert seen == ["teesta__s001"]  # the emulator was not consulted
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "real_run_not_found"

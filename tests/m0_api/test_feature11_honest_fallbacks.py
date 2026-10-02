"""Feature 11: compare / validation never serve the contract example payloads as real data, a
selected scenario is honoured, and an available emulator-vs-physics comparison keeps its real
layer URL and held-out run ID."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.m0_api import schemas
from backend.m0_api.main import app
from tests.m0_api.test_endpoints import _write_synthetic_compare

API = "/api/v1"
SITE = "teesta"
client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
EXAMPLE_RUN_IDS = set(json.loads((EXAMPLES / "compare.example.json").read_text())["sph_vs_delft3d"]["run_ids"])


def _get(path, schema, **params):
    r = client.get(f"{API}{path}", params=params)
    assert r.status_code == 200, r.text
    schemas.validate(schema, r.json())
    return r.json()


def test_compare_without_artifacts_is_unavailable_not_the_example(data_dir):
    body = _get(f"/compare/{SITE}", "compare.schema.json", scenario_id="teesta__s099")
    assert body["scenario_id"] == "teesta__s099"
    for key in ("sph_vs_delft3d", "emulator_vs_physics"):
        assert body[key]["available"] is False and body[key]["metrics"] == {} and body[key]["layers"] == []
        assert body[key]["unavailable_reason"]
    assert body["sph_vs_delft3d"]["run_ids"] == [] and body["gp_vs_linear"] == {}
    assert not EXAMPLE_RUN_IDS & set(json.dumps(body).split('"'))
    assert body["comparison_available"] is False


def test_compare_without_scenario_is_unavailable(data_dir):
    body = _get(f"/compare/{SITE}", "compare.schema.json")
    assert body["sph_vs_delft3d"]["available"] is False and body["emulator_vs_physics"]["available"] is False


def test_selected_scenario_uses_its_real_sidecar(data_dir):
    _write_synthetic_compare(data_dir, scenario_id="teesta_s005")
    body = _get(f"/compare/{SITE}", "compare.schema.json", scenario_id="teesta_s005")
    emu = body["emulator_vs_physics"]
    assert emu["available"] is True and emu["held_out_run_id"] == "teesta_s005__delft3d"
    assert emu["layers"] and emu["layers"][0]["url"].startswith(f"{API}/files/{SITE}/emulator/")
    assert client.get(emu["layers"][0]["url"]).status_code == 200
    assert body["sph_vs_delft3d"]["available"] is False  # no paired SPH run invented
    other = _get(f"/compare/{SITE}", "compare.schema.json", scenario_id="teesta_s006")
    assert other["emulator_vs_physics"]["available"] is False


def test_validation_without_report_is_empty_not_the_example(data_dir):
    body = _get(f"/validation/{SITE}", "validation.schema.json")
    assert body["n_runs"] == 0 and body["per_run"] == [] and body["summary"] == {}
    assert body["validation_available"] is False
    hist = _get(f"/validation/{SITE}", "historical_validation.schema.json", event="teesta_2023")
    assert hist["metrics"] == {} and hist["provenance"]["validation_available"] is False


def test_unknown_query_export_is_404(data_dir):
    r = client.get(f"{API}/export/q_20261002T000000Z_ffffff", params={"format": "geojson"})
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "query_not_found"

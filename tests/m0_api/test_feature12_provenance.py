"""Feature 12: no example/placeholder data on real paths, literature is comparison not validation,
version provenance is real, provenance survives every export format, and Teesta-only runtime
behaviour comes from config/run metadata (not hardcoded IDs). Synthetic fixtures only."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.m0_api import run_metadata, schemas, validation_helpers as vh
from backend.m0_api.main import app
from backend.shared import version
from tests.m0_api.test_endpoints import _register_export_query
from tests.m0_api.test_real_query_diagnostics import test_direct_delft3d_query_flags_dem_pit_and_steep_reach as _make_direct  # noqa: F401

API = "/api/v1"
client = TestClient(app)
QID = "q_20261002T000000Z_abc123"


def test_health_reports_real_versions():
    body = client.get(f"{API}/health").json()
    schemas.validate("health.schema.json", body)
    assert body["code_version"] != "mock-0.0.0"
    assert body["code_version"].split("-")[0] == version.code_version()
    assert body["contract_version"] == version.CONTRACT_VERSION


def test_version_info_shape():
    info = version.version_info()
    assert set(info) == {"code_version", "code_dirty", "contract_version"}
    assert info["code_version"] and info["code_dirty"] in (True, False, None)


@pytest.mark.parametrize("path", [
    "teesta/queries/q_x/layers/depth_p50.png", "teesta/anything/extent.geojson",
    "teesta/scene/terrain.bin", "teesta/emulator/delft3d/validation/compare/none__delft3d__depth_diff.png",
])
def test_files_never_serve_placeholder_bytes(data_dir, path):
    r = client.get(f"{API}/files/{path}")
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "file_not_found"


def test_scene3d_missing_is_scene_unavailable(data_dir):
    r = client.get(f"{API}/scene3d/{QID}")
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "scene_unavailable"


def test_literature_is_never_validation(tmp_path):
    res = vh.build_literature_comparison("teesta", {"run_id": "r1"}, tmp_path)
    assert res["is_validation"] is False and res["kind"] == "literature_reconstruction_comparison"
    assert res["poi"] == "chungthang" and len(res["literature"]) == 3  # config, verbatim
    other = vh.build_literature_comparison("demo_valley", {"run_id": "r1"}, tmp_path)
    assert other["available"] is False and other["literature"] == [] and other["poi"] is None


def test_teesta_runtime_overrides_come_from_config(tmp_path):
    site_dir = tmp_path / "teesta"
    assert run_metadata.terrain_dir(site_dir, "teesta_2023_mvp__delft3d") == tmp_path / "teesta_pilot" / "terrain"
    assert run_metadata.terrain_dir(site_dir, "s002__delft3d") == site_dir / "terrain"
    cands = run_metadata.forcing_candidates(site_dir, site_dir / "runs/x/case", "teesta_2023_mvp__delft3d")
    assert cands[0].name == "teesta_2023_mvp_forcing.csv"
    assert all(str(c).startswith(str(tmp_path)) for c in cands)  # never cwd-relative
    assert run_metadata.dam_id("teesta", "teesta_2023_mvp__delft3d") == "south_lhonak"
    assert run_metadata.default_compare_scenario("teesta") == "teesta_2023_mvp"


def test_non_teesta_site_gets_no_teesta_behaviour(tmp_path):
    site_dir = tmp_path / "demo_valley"
    assert run_metadata.terrain_dir(site_dir, "demo_valley_s001__delft3d") == site_dir / "terrain"
    assert run_metadata.input_forcing_note("demo_valley_s001__delft3d") is None
    assert run_metadata.default_compare_scenario("demo_valley") is None
    assert run_metadata.dam_id("demo_valley", "demo_valley_s001__delft3d") != "south_lhonak"


def test_run_meta_overrides_config(tmp_path):
    meta = {"terrain_site_id": "other", "input_forcing_note": "Synthetic forcing for test", "dam_id": "d1"}
    assert run_metadata.terrain_dir(tmp_path / "s", "teesta_2023_mvp__delft3d", meta) == tmp_path / "other" / "terrain"
    assert run_metadata.input_forcing_note("x", meta) == "Synthetic forcing for test"
    assert run_metadata.dam_id("s", "x", meta) == "d1"


def test_direct_query_provenance_carries_versions_and_time(data_dir):
    from backend.m0_api import real_query
    _make_direct(data_dir)
    result = json.loads((data_dir / "teesta" / "queries" / "q_test_0001" / "result.json").read_text())
    prov = result["provenance"]
    assert prov["code_version"] == version.code_version() and prov["contract_version"] == version.CONTRACT_VERSION
    assert prov["generated_at"].endswith("Z") and prov["solver"] == "delft3d"
    assert prov["input_forcing_note"] is None  # not the Teesta MVP run: no South Lhonak text
    schemas.validate("flood_query_response.schema.json", result)
    assert real_query  # imported for the fixture's side effects


def _export(fmt):
    r = client.get(f"{API}/export/{QID}", params={"format": fmt})
    assert r.status_code == 200, r.text
    return r


def test_provenance_in_every_export_format(data_dir):
    qdir = _register_export_query(data_dir, query_id=QID)
    result = {"flags": {"demo_mode": False, "has_placeholders": True}, "method": "delft3d_direct",
              "placeholder_fields": ["dams[0].height_m"], "caveats": [{"id": "clear_water"}],
              "provenance": {"run_ids": ["s1__delft3d"], "parameters": {"scenario_id": "s1"},
                             "generated_at": "2026-10-02T00:00:00Z"}}
    (qdir / "result.json").write_text(json.dumps(result))

    meta = _export("geojson").json()["metadata"]
    assert meta["query_id"] == QID and meta["scenario_id"] == "s1" and meta["run_ids"] == ["s1__delft3d"]
    assert meta["has_placeholders"] is True and meta["placeholder_fields"] == ["dams[0].height_m"]
    assert meta["code_version"] == version.code_version() and meta["caveats"] == ["clear_water"]
    assert meta["validation_status"] == "not validated against observed data"

    kml = _export("kml").text
    assert "<ExtendedData>" in kml and f"<value>{QID}</value>" in kml and "s1__delft3d" in kml

    with zipfile.ZipFile(io.BytesIO(_export("shp").content)) as z:
        shp_meta = json.loads(z.read("provenance.json"))
    assert shp_meta["query_id"] == QID and shp_meta["format"] == "shp" and shp_meta["has_placeholders"] is True

    assert _export("pdf").content[:4] == b"%PDF"


def test_routed_discharge_status_is_not_validation():
    import inspect

    from backend.m3_dflowfm import routed_discharge
    src = inspect.signature(routed_discharge.write_routed_discharge).parameters["validation_status"].default \
        if hasattr(routed_discharge, "write_routed_discharge") else None
    defaults = [p.default for f in vars(routed_discharge).values() if inspect.isfunction(f)
                for p in inspect.signature(f).parameters.values() if p.name == "validation_status"]
    assert "artifact_schema_checked" in defaults + [src]
    example = json.loads(Path("contracts/examples/routed_discharge.example.json").read_text())
    schemas.validate("routed_discharge.schema.json", example)
    assert example["validation_status"] == "artifact_schema_checked"

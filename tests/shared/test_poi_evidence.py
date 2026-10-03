"""Feature 15 phase 2: POI evidence from a SYNTHETIC OSM extract (tmp dirs only; not real OSM data).
Candidates stay pending; ambiguous / no-match / dam POIs stay unresolved; provenance is preserved."""
from __future__ import annotations

import shutil
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import Point

from backend.m3_dflowfm import production_gate
from backend.shared import input_resolution as ir
from backend.shared import poi_evidence as pe

ROOT = Path(__file__).resolve().parents[2]


def _gpkg(path, rows):
    gpd.GeoDataFrame([{"osm_id": o, "kind": k, "name": n, "geometry": Point(x, y)} for o, k, n, x, y in rows],
                     crs="EPSG:4326").to_file(path, driver="GPKG", layer=path.stem)


@pytest.fixture
def world(tmp_path):
    data, reg = tmp_path / "data", tmp_path / "reg"
    exp = data / "teesta" / "exposure"
    exp.mkdir(parents=True)
    reg.mkdir()
    shutil.copy(ROOT / "config/input_resolution/teesta.yaml", reg / "teesta.yaml")
    _gpkg(exp / "places.gpkg", [
        ("node/1", "village", "Lachen", 88.5561, 27.7170),
        ("node/2", "village", "Chungthang", 88.6470, 27.6040),
        ("node/3", "village", "Dikchu", 88.52, 27.40), ("node/4", "hamlet", "Dikchu", 88.53, 27.41),  # ambiguous
        ("node/5", "town", "Rangpoo", 88.53, 27.17),  # different spelling: no match for Rangpo
    ])
    _gpkg(exp / "facilities.gpkg", [
        ("node/10", "hospital", "District Hospital Mangan", 88.5301, 27.5105),
        ("way/11", "bridge", "Sangkalang Bridge", 88.5310, 27.5205),
        ("way/12", "bridge", "Singtam Bridge", 88.50, 27.23), ("way/13", "bridge", "Old Singtam Bridge", 88.501, 27.231),
        ("node/14", "school", "Mangan School", 88.53, 27.51),
    ])
    return data, reg


def test_unique_matches_become_pending_candidates_with_provenance(world):
    data, reg = world
    res = pe.extract("teesta", data_dir=data)
    by = {e["poi_id"]: e for e in res["entries"]}
    assert set(by) == {"lachen", "chungthang", "sangkalang_bridge", "mangan_district_hospital"}
    lachen = by["lachen"]
    assert lachen["proposed_value"] == [88.5561, 27.717] and lachen["basis"] == "direct" and lachen["source_id"] == "src_048"
    ds = lachen["dataset"]
    assert ds["feature_id"] == "node/1" and ds["match_count"] == 1 and ds["layer"] == "places"
    assert ds["path"] == "teesta/exposure/places.gpkg" and len(ds["sha256"]) == 64
    assert 0 < ds["distance_from_placeholder_m"] < 200
    assert by["mangan_district_hospital"]["basis"] == "secondary_dataset"
    assert by["sangkalang_bridge"]["dataset"]["feature_id"] == "way/11"
    assert all(e["decision"] == {"state": "pending", "reviewer": None, "decided_at": None, "rationale": None}
               for e in res["entries"])  # never decided by tooling


def test_ambiguous_no_match_and_dams_stay_unresolved(world):
    data, _ = world
    un = {u["poi_id"]: u for u in pe.extract("teesta", data_dir=data)["unresolved"]}
    assert un["dikchu"]["reason"] == "UNRESOLVED (ambiguous: 2 matches)"
    assert un["dikchu"]["matching_feature_ids"] == ["node/3", "node/4"]
    assert un["rangpo"]["reason"] == "UNRESOLVED (no match)" and un["rangpo"]["match_count"] == 0
    assert "ambiguous" in un["singtam_bridge"]["reason"]
    for dam in ("teesta_v", "teesta_low_dam_iv", "teesta_barrage"):
        assert un[dam]["reason"].startswith("UNRESOLVED (dam / unsupported category")


def test_missing_extract_is_unverifiable_and_writes_nothing(tmp_path, capsys):
    reg = tmp_path / "reg"
    reg.mkdir()
    with pytest.raises(pe.ExtractUnavailable, match="UNVERIFIABLE"):
        pe.extract("teesta", data_dir=tmp_path / "nodata")
    assert pe.main(["teesta", "--data-dir", str(tmp_path / "nodata"), "--write-candidates"]) == 2
    assert not list(reg.iterdir()) and not (ROOT / "config/input_resolution/teesta.poi_candidates.yaml").exists()


def test_written_candidates_validate_report_and_never_apply_while_pending(world):
    data, reg = world
    pe.write_candidates(pe.extract("teesta", data_dir=data), register_dir=reg)
    assert ir.validate("teesta", register_dir=reg, data_dir=data) == []
    rep = ir.report("teesta", register_dir=reg, data_dir=data)
    rows = {r["key"]: r for r in rep["rows"]}
    assert rows["points_of_interest[0].location"]["resolution"] == ir.CANDIDATE
    assert rows["points_of_interest[0].location"]["dataset_check"] == ir.DATASET_VERIFIED
    assert rows["points_of_interest[2].location"]["unresolved_reason"].startswith("UNRESOLVED (ambiguous")
    assert rep["counts"][ir.CANDIDATE] == 3 + 4 and rep["gate_verdict"] == production_gate.BLOCKED
    sites = data.parent / "sites"
    sites.mkdir()
    shutil.copy(ROOT / "sites/teesta.yaml", sites / "teesta.yaml")
    out = ir.apply("teesta", write=True, sites_dir=sites, register_dir=reg, data_dir=data)
    assert out["applied"] == [] and (sites / "teesta.yaml").read_text() == (ROOT / "sites/teesta.yaml").read_text()


def test_changed_dataset_hash_fails_validation_and_missing_is_unverifiable(world):
    data, reg = world
    pe.write_candidates(pe.extract("teesta", data_dir=data), register_dir=reg)
    _gpkg(data / "teesta/exposure/places.gpkg", [("node/1", "village", "Lachen", 88.0, 27.0)])  # extract changed
    assert any("sha256 mismatch" in p for p in ir.validate("teesta", register_dir=reg, data_dir=data))
    elsewhere = data.parent / "other"
    elsewhere.mkdir()
    assert ir.validate("teesta", register_dir=reg, data_dir=elsewhere) == []  # absent file: not a pass, not a failure
    rep = ir.report("teesta", register_dir=reg, data_dir=elsewhere)
    assert {r["dataset_check"] for r in rep["rows"] if r["key"].startswith("points_of_interest") and r["resolution"] == ir.CANDIDATE} \
        == {ir.DATASET_UNVERIFIABLE}


def test_approved_poi_applies_only_where_dataset_verifies_and_rerun_keeps_decision(world):
    data, reg = world
    path = pe.write_candidates(pe.extract("teesta", data_dir=data), register_dir=reg)
    doc = yaml.safe_load(path.read_text())
    doc["entries"][0]["decision"] = {"state": "approved", "reviewer": "Test Reviewer", "decided_at": "2026-10-03",
                                     "rationale": "checked on imagery (test)"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    pe.write_candidates(pe.extract("teesta", data_dir=data), register_dir=reg)  # re-run keeps the decision
    assert ir.decision_of(yaml.safe_load(path.read_text())["entries"][0])["state"] == "approved"
    sites = data.parent / "sites"
    sites.mkdir()
    shutil.copy(ROOT / "sites/teesta.yaml", sites / "teesta.yaml")
    elsewhere = data.parent / "other"
    elsewhere.mkdir()
    skipped = ir.apply("teesta", sites_dir=sites, register_dir=reg, data_dir=elsewhere)
    assert skipped["applied"] == [] and any("UNVERIFIABLE" in s["reason"] for s in skipped["skipped"])
    out = ir.apply("teesta", write=True, sites_dir=sites, register_dir=reg, data_dir=data)
    assert out["applied"] == ["points_of_interest[0].location"]
    sv = yaml.safe_load((sites / "teesta.yaml").read_text())["points_of_interest"][0]["location"]
    assert sv["value"] == [88.5561, 27.717] and sv["status"] == "sourced"
    assert "node/1" in sv["source"] and "Test Reviewer" in sv["source"] and "src_048" in sv["source"]


def test_name_rules():
    assert pe.distinctive_tokens("Sangkalang bridge (Mangan - Dzongu)") == ["sangkalang"]
    assert pe.distinctive_tokens("District Hospital, Mangan") == ["mangan"]
    assert pe.normalize("Teesta  Bazaar") == "teesta bazaar"


def test_null_or_non_string_osm_names_never_crash_or_match(tmp_path):
    """Regression (real WSL extract): a NULL `name` cell reads back as None/NaN and crashed normalize()."""
    exp = tmp_path / "data" / "teesta" / "exposure"
    exp.mkdir(parents=True)
    _gpkg(exp / "places.gpkg", [
        ("node/100", "village", None, 88.5561, 27.7170),   # NULL name exactly at Lachen's position
        ("node/101", "village", "Lachen", 88.5562, 27.7171),
    ])
    _gpkg(exp / "facilities.gpkg", [
        ("node/200", "hospital", None, 88.5301, 27.5105),  # NULL-named hospital near Mangan
        ("way/201", "bridge", None, 88.5310, 27.5205),
    ])
    res = pe.extract("teesta", data_dir=tmp_path / "data")
    ids = {e["dataset"]["feature_id"] for e in res["entries"]}
    assert ids == {"node/101"}  # only the named Lachen row; no candidate from any NULL-named row
    un = {u["poi_id"]: u for u in res["unresolved"]}
    assert un["mangan_district_hospital"]["reason"] == "UNRESOLVED (no match)"
    assert un["sangkalang_bridge"]["reason"] == "UNRESOLVED (no match)"
    assert all("node/200" not in u["matching_feature_ids"] and "node/100" not in u["matching_feature_ids"]
               for u in res["unresolved"])


@pytest.mark.parametrize("value", [None, float("nan"), 42, 3.5, b"Lachen", ["Lachen"]])
def test_normalize_non_strings_to_empty(value):
    assert pe.normalize(value) == ""

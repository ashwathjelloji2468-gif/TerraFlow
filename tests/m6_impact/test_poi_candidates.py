"""Feature 16 P4: optional, generic POI evidence from an OSM exposure extract. Synthetic GeoPackages
and a synthetic non-Teesta site only; no network, no WSL, no real OSM/WorldPop."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import Point

from backend.m0_api import onboarding
from backend.m6_impact import poi_candidates as pc
from backend.shared import data_foundation as df
from backend.shared.site_config import SiteConfig

REPO = Path(__file__).resolve().parents[2]
SID = "kosi_reach"


def _cfg(pois=None) -> SiteConfig:
    raw = yaml.safe_load((REPO / "sites" / "synth_engdam.yaml").read_text(encoding="utf-8"))
    raw["site"]["id"] = SID
    loc = lambda lon, lat: {"value": [lon, lat], "unit": "deg", "source": "synthetic placeholder", "status": "placeholder"}  # noqa: E731
    raw["points_of_interest"] = pois or [
        {"id": "alpha", "name": "Alphaville", "category": "village", "location": loc(88.50, 27.50)},
        {"id": "beta", "name": "Beta District Hospital", "category": "hospital", "location": loc(88.51, 27.51)},
        {"id": "gamma", "name": "Gamma Bridge", "category": "bridge", "location": loc(88.52, 27.52)},
        {"id": "twin", "name": "Twin", "category": "village", "location": loc(88.53, 27.53)},
        {"id": "nomatch", "name": "Nowhere", "category": "village", "location": loc(88.54, 27.54)},
        {"id": "main_dam_poi", "name": "Main Dam", "category": "dam", "location": loc(88.55, 27.55)},
    ]
    return SiteConfig.model_validate(raw)


def _gpkg(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame([{"osm_id": r[0], "kind": r[1], "name": r[2]} for r in rows],
                     geometry=[Point(r[3], r[4]) for r in rows], crs="EPSG:4326").to_file(path, driver="GPKG")


@pytest.fixture
def extract_dir(tmp_path):
    exp = tmp_path / SID / "exposure"
    _gpkg(exp / "places.gpkg", [
        ("node/1", "village", "Alphaville", 88.501, 27.501),
        ("node/2", "village", "Twin", 88.53, 27.53), ("node/3", "hamlet", "Twin", 88.531, 27.531),
        ("node/4", "village", None, 88.6, 27.6),  # NULL name never matches
        ("node/5", "village", "Main Dam", 88.55, 27.55),  # a settlement named like the dam: still not used
    ])
    _gpkg(exp / "facilities.gpkg", [
        ("way/10", "hospital", "Beta Hospital", 88.511, 27.511),
        ("way/11", "bridge", "Gamma Bridge", 88.521, 27.521),
        ("way/12", "school", "Beta School", 88.5, 27.5),
    ])
    return tmp_path


def _by(result):
    return {e["poi_id"]: e for e in result["candidates"] + result["unresolved"]}


def test_supported_categories_produce_pending_candidates_with_provenance(extract_dir):
    res = pc.extract(_cfg(), data_dir=extract_dir)
    e = _by(res)
    for pid, layer, fid in (("alpha", "places", "node/1"), ("beta", "facilities", "way/10"), ("gamma", "facilities", "way/11")):
        c = e[pid]
        assert c["status"] == "CANDIDATE" and c["review_state"] == "pending"
        d = c["dataset"]
        assert d["layer"] == layer and d["feature_id"] == fid and d["match_count"] == 1 and d["match_rule"]
        sha = hashlib.sha256((extract_dir / SID / d["path"]).read_bytes()).hexdigest()
        assert d["sha256"] == sha and d["path"] == f"exposure/{layer}.gpkg"
        assert d["distance_from_current_value_m"] > 0 and c["source_id"] == "src_048"
    assert e["alpha"]["basis"] == "direct" and e["beta"]["basis"] == "secondary_dataset"
    assert "not an authoritative source" in res["authority"]


def test_ambiguous_and_no_match_stay_unresolved(extract_dir):
    e = _by(pc.extract(_cfg(), data_dir=extract_dir))
    assert e["twin"]["status"] == "UNRESOLVED" and e["twin"]["reason"] == "ambiguous: 2 matches"
    assert e["twin"]["matching_feature_ids"] == ["node/2", "node/3"] and "proposed_value" not in e["twin"]
    assert e["nomatch"]["status"] == "UNRESOLVED" and e["nomatch"]["reason"] == "no match"


def test_dam_poi_remains_unresolved(extract_dir):
    d = _by(pc.extract(_cfg(), data_dir=extract_dir))["main_dam_poi"]
    assert d["status"] == "UNRESOLVED" and d["reason"].startswith("dam / unsupported category")
    assert "proposed_value" not in d and d["match_count"] == 0


def test_never_auto_approved(extract_dir):
    res = pc.extract(_cfg(), data_dir=extract_dir)
    records = res["candidates"] + res["unresolved"]
    assert {r["status"] for r in records} == {"CANDIDATE", "UNRESOLVED"}
    assert all(r["review_state"] == "pending" for r in res["candidates"])
    assert not any(k in r for r in records for k in ("approval", "approved_by", "decision"))


def test_missing_extract_is_unverifiable_and_writes_nothing(tmp_path):
    out = pc.run(_cfg(), data_dir=tmp_path)
    assert out["status"] == "UNVERIFIABLE" and out["written"] is False and "places.gpkg" in out["reason"]
    assert not (tmp_path / SID).exists()


def test_unreadable_extract_is_unverifiable(tmp_path):
    exp = tmp_path / SID / "exposure"
    exp.mkdir(parents=True)
    (exp / "places.gpkg").write_bytes(b"not a geopackage")
    (exp / "facilities.gpkg").write_bytes(b"not a geopackage")
    assert pc.run(_cfg(), data_dir=tmp_path)["status"] == "UNVERIFIABLE"
    assert not (exp / "poi_evidence.json").exists()


def test_missing_extract_keeps_existing_evidence(extract_dir):
    pc.run(_cfg(), data_dir=extract_dir)
    ev = extract_dir / SID / "exposure" / "poi_evidence.json"
    before = ev.read_bytes()
    (extract_dir / SID / "exposure" / "places.gpkg").unlink()
    assert pc.run(_cfg(), data_dir=extract_dir)["status"] == "UNVERIFIABLE"
    assert ev.read_bytes() == before


def test_exposure_data_preserved_and_deterministic(extract_dir):
    exp = extract_dir / SID / "exposure"
    layers = {p.name: p.read_bytes() for p in exp.glob("*.gpkg")}
    first = pc.run(_cfg(), data_dir=extract_dir)
    text = (exp / "poi_evidence.json").read_bytes()
    second = pc.run(_cfg(), data_dir=extract_dir)
    assert first["written"] is True and second["written"] is False
    assert (exp / "poi_evidence.json").read_bytes() == text
    assert {p.name: p.read_bytes() for p in exp.glob("*.gpkg")} == layers
    assert "generated_at" not in text.decode() and str(extract_dir) not in text.decode()


@pytest.mark.parametrize("bad", ["../x", "Teesta", "a/b", "", None])
def test_unsafe_site_id_rejected(tmp_path, bad):
    with pytest.raises(pc.UnsafeEvidencePath):
        pc.site_paths(bad, tmp_path)


def test_symlinked_layer_outside_site_rejected(tmp_path):
    outside = tmp_path / "elsewhere.gpkg"
    _gpkg(outside, [("node/1", "village", "Alphaville", 88.5, 27.5)])
    exp = tmp_path / "data" / SID / "exposure"
    exp.mkdir(parents=True)
    (exp / "places.gpkg").symlink_to(outside)
    with pytest.raises(pc.UnsafeEvidencePath):
        pc.extract(_cfg(), data_dir=tmp_path / "data")


def test_symlinked_exposure_dir_to_other_site_rejected(extract_dir):
    other = extract_dir / "other_site"
    other.mkdir()
    (other / "exposure").symlink_to(extract_dir / SID / "exposure", target_is_directory=True)
    raw = _cfg().model_dump(by_alias=True)
    raw["site"]["id"] = "other_site"
    with pytest.raises(pc.UnsafeEvidencePath):
        pc.run(SiteConfig.model_validate(raw), data_dir=extract_dir)  # cannot write into kosi_reach's data


def test_no_teesta_values_leak(extract_dir):
    pc.run(_cfg(), data_dir=extract_dir)
    text = (extract_dir / SID / "exposure" / "poi_evidence.json").read_text().lower()
    for word in ("teesta", "lhonak", "chungthang", "mangan", "singtam"):
        assert word not in text, word


def test_repo_registers_and_site_configs_untouched(extract_dir):
    def snap(root):
        return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    before = snap(REPO / "config" / "input_resolution") | snap(REPO / "sites")
    pc.run(_cfg(), data_dir=extract_dir)
    assert snap(REPO / "config" / "input_resolution") | snap(REPO / "sites") == before


# --- optional onboarding hook ------------------------------------------------------------------
def test_disabled_by_default():
    assert onboarding.poi_evidence_enabled() is False
    raw = yaml.safe_load((REPO / "config" / "onboarding.yaml").read_text())
    assert raw["poi_evidence"]["enabled"] is False


@pytest.mark.parametrize("text,expected", [("poi_evidence: {enabled: true}", True), ("poi_evidence: {enabled: 'yes'}", False),
                                          ("", False), ("poi_evidence: {}", False)])
def test_enable_flag_requires_explicit_true(tmp_path, text, expected):
    p = tmp_path / "o.yaml"
    p.write_text(text)
    assert onboarding.poi_evidence_enabled(p) is expected


def test_hook_off_does_nothing(extract_dir):
    assert onboarding.prepare_poi_evidence(_cfg(), extract_dir) is None
    assert not (extract_dir / SID / "exposure" / "poi_evidence.json").exists()


def test_hook_on_without_extract_never_fails(tmp_path):
    events = []
    out = onboarding.prepare_poi_evidence(_cfg(), tmp_path, events.append, enabled=True)
    assert out["status"] == "UNVERIFIABLE" and "onboarding continues" in events[0]


def test_hook_on_with_extract_writes_pending_evidence(extract_dir):
    events = []
    out = onboarding.prepare_poi_evidence(_cfg(), extract_dir, events.append, enabled=True)
    assert out["written"] is True and out["counts"] == {"CANDIDATE": 3, "UNRESOLVED": 3}
    assert "nothing approved" in events[0]


def test_hook_swallows_unexpected_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    events = []
    assert onboarding.prepare_poi_evidence(_cfg(), tmp_path, events.append, enabled=True)["status"] == "error"
    assert events and events[0].startswith("poi evidence: skipped")


# --- Data Foundation view ------------------------------------------------------------------------
def _site_files(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir(exist_ok=True)
    (sites / f"{SID}.yaml").write_text(yaml.safe_dump(_cfg().model_dump(by_alias=True), sort_keys=False))
    return sites


def test_data_foundation_reports_evidence(extract_dir):
    pc.run(_cfg(), data_dir=extract_dir)
    rep = df.build(SID, data_dir=extract_dir, sites_dir=_site_files(extract_dir), register_dir=extract_dir / "reg",
                   checklist_path=False)
    poi = rep["I_poi_evidence"]
    assert poi["file"] == "PRESENT" and poi["counts"] == {"CANDIDATE": 3, "UNRESOLVED": 3}
    assert poi["dataset_checks"] == {"facilities": "VERIFIED", "places": "VERIFIED"}
    assert {c["review_state"] for c in poi["candidates"]} == {"pending"}
    # the register view is unchanged by evidence: POI values stay PLACEHOLDER / UNRESOLVED
    v = {x["key"]: x for x in rep["B_values"]["items"]}
    assert v["points_of_interest[0].location"]["status"] == "PLACEHOLDER"
    assert v["points_of_interest[0].location"]["resolution"] == "UNRESOLVED"


def test_data_foundation_without_evidence_and_tampered_paths(extract_dir):
    sites = _site_files(extract_dir)
    rep = df.build(SID, data_dir=extract_dir, sites_dir=sites, register_dir=extract_dir / "reg", checklist_path=False)
    assert rep["I_poi_evidence"]["file"] == "MISSING"
    pc.run(_cfg(), data_dir=extract_dir)
    ev = extract_dir / SID / "exposure" / "poi_evidence.json"
    data = json.loads(ev.read_text())
    data["datasets"]["places"]["path"] = "../../../etc/passwd"
    data["datasets"]["facilities"]["sha256"] = "0" * 64
    ev.write_text(json.dumps(data))
    rep = df.build(SID, data_dir=extract_dir, sites_dir=sites, register_dir=extract_dir / "reg", checklist_path=False)
    assert rep["I_poi_evidence"]["dataset_checks"] == {"facilities": "MISMATCH", "places": "UNVERIFIABLE"}

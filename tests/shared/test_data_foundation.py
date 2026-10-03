"""Feature 16 P2: read-only Data Foundation view. Synthetic fixtures only (a renamed copy of the
`synth_engdam` test config); no network, no solver, no WSL data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from backend.shared import data_foundation as df
from backend.shared import input_resolution as ir

REPO = Path(__file__).resolve().parents[2]
SID = "synth_df"
from backend.m1_terrain.discharge import DATASET as DIS_DATASET  # noqa: E402

QUOTE = {"source_id": "src_043", "quote": "60 m maximum height above riverbed"}  # verbatim in docs/data_sources.md


def _site_yaml() -> dict:
    cfg = yaml.safe_load((REPO / "sites" / "synth_engdam.yaml").read_text(encoding="utf-8"))
    cfg["site"]["id"] = SID
    bi = cfg["dams"][0]["breach_inputs"]
    for k in ("dam_height", "breach_height", "water_height_above_invert", "average_embankment_width"):
        bi[k].update(status="placeholder", source="Synthetic placeholder for the P2 test")
    return cfg


@pytest.fixture
def world(tmp_path):
    sites, reg_dir, data = tmp_path / "sites", tmp_path / "register", tmp_path / "data"
    sites.mkdir(); reg_dir.mkdir(); data.mkdir()
    (sites / f"{SID}.yaml").write_text(yaml.safe_dump(_site_yaml(), sort_keys=False), encoding="utf-8")
    base = {"unit": "m", "basis": "direct", **QUOTE}
    reg = {"site_id": SID, "entries": [
        {**base, "target": "dams[0].breach_inputs.dam_height", "proposed_value": 25,
         "approval": {"approved_by": "Reviewer A", "approved_at": "2026-10-03"}},
        {**base, "target": "dams[0].breach_inputs.breach_height", "proposed_value": 20,
         "approval": {"approved_by": None, "approved_at": None}},
        {**base, "target": "dams[0].breach_inputs.water_height_above_invert", "proposed_value": 18,
         "approval": {"approved_by": None, "approved_at": None}, "decision": {"state": "rejected"}},
    ], "evidence_only": []}
    (reg_dir / f"{SID}.yaml").write_text(yaml.safe_dump(reg, sort_keys=False), encoding="utf-8")
    return {"sites": sites, "reg": reg_dir, "data": data}


def _build(w, **kw):
    return df.build(SID, data_dir=w["data"], sites_dir=w["sites"], register_dir=w["reg"], checklist_path=False, **kw)


def _values(rep):
    return {v["key"]: v for v in rep["B_values"]["items"]}


def test_synthetic_non_teesta_site_loads(world):
    rep = _build(world)
    a = rep["A_site"]
    assert a["config_loaded"] and a["config_origin"] == "explicit" and a["config_path"] == f"<sites_dir>/{SID}.yaml"
    assert set(rep) >= {"A_site", "B_values", "C_terrain_provenance", "D_terrain_artifacts", "E_hydrology",
                        "F_input_resolution", "G_blocking", "H_warnings"}
    assert rep["read_only"] is True


def test_placeholder_values_reported_as_placeholder(world):
    v = _values(_build(world))["dams[0].breach_inputs.dam_height"]
    assert v["status"] == "PLACEHOLDER" and v["config_status"] == "placeholder" and v["unit"] == "m"
    sourced = [x for x in _values(_build(world)).values() if x["config_status"] == "sourced"]
    assert sourced and all(x["status"] == "READY" and x["resolution"] is None for x in sourced)


def test_approved_candidate_unresolved_preserved(world):
    rep = _build(world)
    v = _values(rep)
    assert v["dams[0].breach_inputs.dam_height"]["resolution"] == "APPROVED"
    assert v["dams[0].breach_inputs.breach_height"]["resolution"] == "CANDIDATE"
    unresolved = [x for x in v.values() if x["status"] != "READY" and x["blocking"] and x["resolution"] == "UNRESOLVED"]
    assert unresolved  # values with no register entry stay UNRESOLVED
    assert rep["F_input_resolution"]["counts"] == {s: sum(1 for r in rep["F_input_resolution"]["rows"] if r["resolution"] == s)
                                                   for s in rep["F_input_resolution"]["counts"]}


def test_rejected_state_passed_through_verbatim(world, monkeypatch):
    """REJECTED comes from the register tooling (decision records); the view never re-derives it."""
    original = ir.entry_status
    monkeypatch.setattr(ir, "entry_status",
                        lambda e: "REJECTED" if (e.get("decision") or {}).get("state") == "rejected" else original(e))
    rep = _build(world)
    assert _values(rep)["dams[0].breach_inputs.water_height_above_invert"]["resolution"] == "REJECTED"
    g = {i["key"]: i for i in rep["G_blocking"]["items"]}
    assert g["dams[0].breach_inputs.water_height_above_invert"]["resolution"] == "REJECTED"


def test_approved_entry_is_not_applied(world):
    """APPROVED in the register does not make the value READY: only `apply` + review changes config."""
    v = _values(_build(world))["dams[0].breach_inputs.dam_height"]
    assert v["resolution"] == "APPROVED" and v["status"] == "PLACEHOLDER"


def test_missing_provenance_and_artifacts_are_explicit(world):
    rep = _build(world)
    c, d = rep["C_terrain_provenance"], rep["D_terrain_artifacts"]
    assert c["raw_provenance"] == "MISSING" and c["products"] == [] and c["dem_selection"]["file"] == "MISSING"
    assert d["terrain_provenance"] == "MISSING" and {f["state"] for f in d["files"]} == {"MISSING"}
    assert d["gate_item"]["status"] == "BLOCKED" and "missing" in d["gate_item"]["detail"]
    assert rep["E_hydrology"]["discharge_glofas"]["status"] is None
    w = " ".join(rep["H_warnings"])
    assert "raw/provenance.json) is MISSING" in w and "terrain/provenance.json) is MISSING" in w
    assert "terrain_artifacts" in {i["key"] for i in rep["G_blocking"]["items"]}


def _write_raw(w, *, corrupt=False):
    raw = w["data"] / SID / "raw"
    raw.mkdir(parents=True)
    dem = raw / "dem_copernicus_glo30.tif"
    dem.write_bytes(b"synthetic dem bytes")
    csv = raw / "discharge_glofas.csv"
    csv.write_text("date,river_discharge_m3s\n2020-01-01,1.0\n")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    prov = {
        "dem_copernicus_glo30": {"status": "fetched", "dataset": "copernicus_glo30", "source": "src_034",
                                 "file": dem.name, "sha256": "0" * 64 if corrupt else sha(dem)},
        "dem_srtm_gl1": {"status": "unavailable", "dataset": "srtm_gl1", "reason": "network ingestion is disabled"},
        "landcover_esa_worldcover": {"status": "fetched", "file": "landcover.tif", "sha256": "1" * 64},
        "hydrobasins_catchment": {"status": "fetched", "file": "catchment.geojson", "sha256": "2" * 64},
        "discharge_glofas": {"status": "fetched", "dataset": DIS_DATASET, "source": "src_075",
                             "file": csv.name, "sha256": sha(csv), "kind": "modelled (GloFAS reanalysis / archived forecast), not gauged",
                             "summary_m3s": {"median": 1.0}},
    }
    (raw / "provenance.json").write_text(json.dumps(prov))
    (raw / "dem_comparison.json").write_text(json.dumps({"selected": "copernicus_glo30", "threshold_met": True}))


def test_provenance_file_checks(world):
    _write_raw(world)
    rep = _build(world)
    prods = {p["key"]: p for p in rep["C_terrain_provenance"]["products"]}
    assert prods["dem_copernicus_glo30"]["file_check"]["status"] == "VERIFIED"
    assert prods["dem_srtm_gl1"]["status"] == "unavailable" and prods["dem_srtm_gl1"]["file_check"] is None
    assert prods["dem_srtm_gl1"]["reason"] == "network ingestion is disabled"  # quoted verbatim
    assert prods["landcover_esa_worldcover"]["file_check"]["status"] == "UNVERIFIABLE"  # file absent
    hb = rep["E_hydrology"]["hydrobasins_catchment"]
    assert hb["file_check"]["status"] == "UNVERIFIABLE"  # sha is of the source shapefile, not the output
    assert rep["C_terrain_provenance"]["dem_selection"]["selected"] == "copernicus_glo30"


def test_hash_mismatch_flagged(world):
    _write_raw(world, corrupt=True)
    rep = _build(world)
    prods = {p["key"]: p for p in rep["C_terrain_provenance"]["products"]}
    assert prods["dem_copernicus_glo30"]["file_check"]["status"] == "MISMATCH"
    assert any("MISMATCH" in w for w in rep["H_warnings"])


def test_glofas_stays_labelled_modelled(world):
    _write_raw(world)
    rep = _build(world)
    q = rep["E_hydrology"]["discharge_glofas"]
    assert q["modelled_not_gauged"] is True and q["kind"].startswith("modelled") and q["file_check"]["status"] == "VERIFIED"
    assert "not gauged" in rep["E_hydrology"]["note"]
    bf = rep["E_hydrology"]["config_base_flow"]
    assert bf is None or bf["status"] != "READY" or bf["config_status"] == "sourced"  # never filled from GloFAS


def test_deterministic(world):
    _write_raw(world)
    a, b = _build(world), _build(world)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert df.to_markdown(a) == df.to_markdown(b)
    assert "evaluated_at" not in json.dumps(a)


def _tree_hash(*roots):
    h = hashlib.sha256()
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            h.update(str(p).encode())
            if p.is_file():
                h.update(p.read_bytes())
    return h.hexdigest()


def test_read_only(world):
    _write_raw(world)
    before = _tree_hash(world["sites"], world["reg"], world["data"])
    repo_before = _tree_hash(REPO / "sites", REPO / "config" / "input_resolution")
    _build(world)
    df.build("teesta", data_dir=world["data"])
    assert _tree_hash(world["sites"], world["reg"], world["data"]) == before
    assert _tree_hash(REPO / "sites", REPO / "config" / "input_resolution") == repo_before


def test_no_teesta_defaults_leak_into_synthetic_site(world):
    _write_raw(world)
    text = json.dumps(_build(world), default=str).lower()
    for word in ("teesta", "lhonak", "chungthang", "src_048"):
        assert word not in text, word


def test_teesta_remains_blocked(tmp_path):
    rep = df.build("teesta", data_dir=tmp_path)
    assert rep["A_site"]["config_origin"] == "bundled" and rep["G_blocking"]["gate_verdict"] == "BLOCKED"
    v = _values(rep)
    assert v["domains.far_field.inflow.base_flow"]["status"] == "BLOCKED"
    assert v["domains.far_field.inflow.base_flow"]["resolution"] == "UNRESOLVED"
    assert rep["F_input_resolution"]["counts"]["APPROVED"] == 0  # nothing approved by tooling
    assert rep["F_input_resolution"]["valid"] is True


def test_unknown_site_is_explicit(tmp_path):
    rep = df.build("no_such_site", data_dir=tmp_path)
    assert rep["A_site"]["config_loaded"] is False and rep["G_blocking"]["gate_verdict"] == "BLOCKED"
    assert rep["B_values"]["items"] == [] and "error" in rep["A_site"]


def test_onboarded_config_location(tmp_path):
    cfg_dir = tmp_path / SID / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / f"{SID}.yaml").write_text(yaml.safe_dump(_site_yaml(), sort_keys=False), encoding="utf-8")
    rep = df.build(SID, data_dir=tmp_path, checklist_path=False)
    assert rep["A_site"]["config_origin"] == "onboarded"
    assert rep["A_site"]["config_path"] == f"<data_dir>/{SID}/config/{SID}.yaml"
    assert rep["F_input_resolution"]["register_present"] is False
    assert any("no input-resolution register" in w for w in rep["H_warnings"])


def test_cli_markdown_and_json(world, capsys, monkeypatch):
    real = df.build
    monkeypatch.setattr(df, "build", lambda sid, data_dir=None: real(
        sid, data_dir=world["data"], sites_dir=world["sites"], register_dir=world["reg"], checklist_path=False))
    assert df.main([SID, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["site_id"] == SID
    assert df.main([SID]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"# Data Foundation: `{SID}`") and "## H. Provenance warnings" in out

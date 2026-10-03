"""Feature 16 P6: end-to-end generalization proof on a SYNTHETIC, non-Teesta site.

`tests/fixtures/f16/synth_river_alpha.yaml` (not real data, UTM zone 43N, no Teesta ids/sources/
coordinates) goes through: site-config validation -> POST /sites + the onboarding worker (M1 terrain
from a synthetic valley DEM, M2 breach, M5 design, campaign case preparation; no solver run, no
network) -> generic register initialisation -> optional POI evidence (synthetic GeoPackages) ->
GloFAS base-flow candidate (synthetic CSV) -> read-only Data Foundation report -> a leak audit of
everything written for the site. Teesta's config/register/report are checked unchanged."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import warnings
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import Point

from backend.m0_api import onboarding
from backend.m0_api.main import app
from backend.m0_api.worker import Worker
from backend.m1_terrain import base_flow_candidates as bfc
from backend.m1_terrain import discharge, download
from backend.shared import data_foundation as df
from backend.shared import input_resolution_init as rinit
from backend.shared.site_config import PlaceholderWarning, SiteConfig, load_site_config
from tests.m0_api.conftest import wait_until
from tests.m1_terrain import synthetic_valley as valley

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "f16" / "synth_river_alpha.yaml"
SID = "synth_river_alpha"
UTM = 32643
LEAK_WORDS = ("teesta", "south lhonak", "south_lhonak", "lhonak", "chungthang", "teesta iii", "teesta_iii",
              "src_043", "src_073", "src_048", "teesta_2023", "32645", "mangan", "singtam")
TEXT_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".mdu", ".ext", ".xml", ".pli", ".txt", ".md", ".geojson", ".bc", ".ini"}


def _raw() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _teesta_coordinate_strings() -> set[str]:
    """Every lon/lat literal in sites/teesta.yaml with >= 2 decimals (e.g. '88.625')."""
    text = (REPO / "sites" / "teesta.yaml").read_text(encoding="utf-8")
    return {m for m in re.findall(r"\b(?:8[6-9]|2[6-9])\.\d{2,}\b", text)}


def _snapshot(*paths: Path) -> dict[str, str]:
    out = {}
    for root in paths:
        for p in sorted([root] if root.is_file() else root.rglob("*")):
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _seed_valley(data_dir: Path, raw: dict, monkeypatch) -> None:
    """The repository's synthetic V-valley DEM/landcover, generated in this site's UTM zone."""
    monkeypatch.setattr(valley, "UTM_EPSG", UTM)
    monkeypatch.setattr(valley, "_TO_UTM", Transformer.from_crs(4326, UTM, always_xy=True))
    monkeypatch.setattr(valley, "_TO_LONLAT", Transformer.from_crs(UTM, 4326, always_xy=True))
    dam = raw["dams"][0]
    raw_dir = data_dir / SID / "raw"
    valley.write_raw_rasters(raw_dir, tuple(raw["domains"]["far_field"]["bbox"]["value"]),
                             tuple(dam["breach_location"]["value"]),
                             tuple(raw["points_of_interest"][0]["location"]["value"]), tuple(dam["location"]["value"]))
    valley.write_raw_provenance(raw_dir, "srtm_gl1")


def _gpkg(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame([{"osm_id": r[0], "kind": r[1], "name": r[2]} for r in rows],
                     geometry=[Point(r[3], r[4]) for r in rows], crs="EPSG:4326").to_file(path, driver="GPKG")


def _seed_glofas(data_dir: Path, inflow: list[float]) -> None:
    raw_dir = data_dir / SID / "raw"
    values = [9.0, 11.0, None, 10.0, 14.0, 8.0]
    csv_path = raw_dir / discharge.OUTPUT_NAME
    csv_path.write_text("date,river_discharge_m3s\n" + "".join(
        f"2021-03-{i + 1:02d},{'' if v is None else v}\n" for i, v in enumerate(values)))
    valid = [v for v in values if v is not None]
    download._merge_provenance(raw_dir, "discharge_glofas", {
        "file": discharge.OUTPUT_NAME, "status": "fetched", "dataset": discharge.DATASET, "source": discharge.SOURCE_ID,
        "service_url": discharge.SERVICE_URL, "kind": "modelled (GloFAS reanalysis / archived forecast), not gauged",
        "request_point_lonlat": inflow, "point_basis": "domains.far_field.inflow.location",
        "returned_point_lonlat": inflow, "period": ["2021-03-01", "2021-03-06"], "n_days": 6, "n_missing": 1,
        "units": "m^3/s", "time_basis": "daily, UTC dates", "license": discharge.LICENSE,
        "summary_m3s": {"min": min(valid), "p10": discharge._percentile(valid, 0.1), "median": statistics.median(valid),
                        "mean": statistics.fmean(valid), "p90": discharge._percentile(valid, 0.9), "max": max(valid)},
        "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(), "size_bytes": csv_path.stat().st_size,
        "fetched_at": "2021-03-07T00:00:00+00:00"})


def test_fixture_is_synthetic_and_teesta_free():
    raw = _raw()
    with warnings.catch_warnings():
        warnings.simplefilter("error", PlaceholderWarning)  # fully sourced (synthetic) -> no placeholder banner
        cfg = load_site_config(SID, sites_dir=FIXTURE.parent)
    assert cfg.site.id == SID and cfg.crs.utm_epsg.value == UTM and cfg.placeholder_fields == []
    text = FIXTURE.read_text(encoding="utf-8").lower()
    for word in LEAK_WORDS:
        assert word not in text, word
    assert not _teesta_coordinate_strings() & set(re.findall(r"\d+\.\d+", text))
    lons = [raw["domains"]["far_field"]["bbox"]["value"][i] for i in (0, 2)]
    assert all(72 <= x < 78 for x in lons)  # UTM zone 43N, outside the Teesta box (88.10-88.85 E)
    sources = {sv["source"] for sv in json.loads(json.dumps(raw)).get("dams")[0]["breach_inputs"].values()}
    assert sources == {"Synthetic generalization fixture (Feature 16 P6) - not real data"}


def test_synthetic_site_end_to_end(data_dir, tmp_path, monkeypatch):
    teesta_before = _snapshot(REPO / "sites" / "teesta.yaml", REPO / "config" / "input_resolution",
                              REPO / "docs" / "teesta_input_resolution.md")
    raw = _raw()
    _seed_valley(data_dir, raw, monkeypatch)

    # 1. onboarding through the API + worker (M1 terrain, M2 breach, M5 design, campaign prep) ------
    client = TestClient(app)
    accepted = client.post("/api/v1/sites", json={"site_config": raw, "demo_mode": True})
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    worker = Worker()
    worker.acquire_lock()
    try:
        wait_until(lambda: client.get(f"/api/v1/jobs/{job_id}").json()["stage"] in {"simulating", "failed"},
                   worker.tick, timeout_s=60)
    finally:
        worker.close()
    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["stage"] == "simulating", status.get("error")
    site = data_dir / SID
    grid = json.loads((site / "terrain" / "grid.json").read_text())
    assert grid["crs_epsg"] == UTM
    assert (site / "breach" / "breach_params.json").is_file() and (site / "design" / "scenario_design.json").is_file()
    assert not (site / "exposure" / "poi_evidence.json").exists()  # optional POI step is OFF by default
    cfg_path = site / "config" / f"{SID}.yaml"
    cfg_bytes = cfg_path.read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        cfg = SiteConfig.model_validate(yaml.safe_load(cfg_bytes))

    # 2. generic register initialisation ---------------------------------------------------------------
    reg_dir = tmp_path / "register"
    first = rinit.init_register(SID, data_dir=data_dir, register_dir=reg_dir)
    assert first["status"] == "created" and first["problems"] == []
    reg_bytes = (reg_dir / f"{SID}.yaml").read_bytes()
    assert yaml.safe_load(reg_bytes) == {"site_id": SID, "entries": [], "evidence_only": []}
    assert rinit.init_register(SID, data_dir=data_dir, register_dir=reg_dir)["status"] == "exists"
    assert (reg_dir / f"{SID}.yaml").read_bytes() == reg_bytes
    other = rinit.init_register(SID, data_dir=data_dir, register_dir=tmp_path / "register2")
    assert other["status"] == "created" and (tmp_path / "register2" / f"{SID}.yaml").read_bytes() == reg_bytes

    # 3. optional POI evidence on synthetic GeoPackages ----------------------------------------------------
    _gpkg(site / "exposure" / "places.gpkg", [("node/101", "town", "Alpha Town", 75.091, 32.091),
                                               ("node/102", "village", "Alpha Dam", 75.06, 32.145)])
    _gpkg(site / "exposure" / "facilities.gpkg", [("way/201", "bridge", "Alpha Bridge", 75.12, 32.07),
                                                   ("way/202", "bridge", "Alpha Bridge", 75.121, 32.071)])
    extract_hashes = _snapshot(site / "exposure")
    poi = onboarding.prepare_poi_evidence(cfg, data_dir, enabled=True)
    assert poi["status"] == "COMPLETE" and poi["counts"] == {"CANDIDATE": 1, "UNRESOLVED": 2}
    ev_path = site / "exposure" / "poi_evidence.json"
    ev = json.loads(ev_path.read_text())
    by = {e["poi_id"]: e for e in ev["candidates"] + ev["unresolved"]}
    assert by["alpha_town"]["status"] == "CANDIDATE" and by["alpha_town"]["review_state"] == "pending"
    assert by["alpha_town"]["dataset"]["feature_id"] == "node/101" and len(by["alpha_town"]["dataset"]["sha256"]) == 64
    assert by["alpha_bridge"]["status"] == "UNRESOLVED" and by["alpha_bridge"]["reason"] == "ambiguous: 2 matches"
    assert by["alpha_dam_site"]["status"] == "UNRESOLVED" and "dam" in by["alpha_dam_site"]["reason"]
    ev_bytes = ev_path.read_bytes()
    onboarding.prepare_poi_evidence(cfg, data_dir, enabled=True)
    assert ev_path.read_bytes() == ev_bytes  # deterministic, idempotent
    assert {k: v for k, v in _snapshot(site / "exposure").items() if not k.endswith("poi_evidence.json")} == extract_hashes

    # 4. GloFAS base-flow candidate (synthetic CSV; configured base_flow 12.0 m^3/s is preserved) -------
    _seed_glofas(data_dir, list(cfg.domains.far_field.inflow.location.value))
    hydro = bfc.run(cfg, data_dir=data_dir)
    rec = hydro["record"]
    assert hydro["status"] == "CANDIDATE" and rec["review_state"] == "pending" and rec["proposed_value"] is None
    assert rec["modelled_not_gauged"] is True and rec["unit"] == "m^3/s" and rec["statistics_m3s"]["median"] == 10.0
    assert rec["configured_base_flow"]["value"] == 12.0 and rec["configured_base_flow"]["status"] == "sourced"
    bf_bytes = (site / "raw" / bfc.OUTPUT_NAME).read_bytes()
    assert bfc.run(cfg, data_dir=data_dir)["written"] is False
    assert (site / "raw" / bfc.OUTPUT_NAME).read_bytes() == bf_bytes

    # 5. Data Foundation (read-only, deterministic) ---------------------------------------------------------
    before = _snapshot(site, reg_dir)
    rep = df.build(SID, data_dir=data_dir, register_dir=reg_dir)
    rep2 = df.build(SID, data_dir=data_dir, register_dir=reg_dir)
    assert json.dumps(rep, sort_keys=True, default=str) == json.dumps(rep2, sort_keys=True, default=str)
    assert df.to_markdown(rep) == df.to_markdown(rep2)
    assert _snapshot(site, reg_dir) == before
    a = rep["A_site"]
    assert a["config_origin"] == "onboarded" and a["name"].startswith("Synthetic River Alpha") and a["dams"] == ["alpha_dam"]
    assert rep["B_values"]["counts"]["PLACEHOLDER"] == 0 and rep["B_values"]["counts"]["BLOCKED"] == 0
    assert rep["D_terrain_artifacts"]["gate_item"]["status"] == "READY"
    assert rep["D_terrain_artifacts"]["terrain_provenance"] == "PRESENT"
    assert rep["C_terrain_provenance"]["dem_selection"]["selected"] == "srtm_gl1"
    assert rep["F_input_resolution"]["register_present"] and rep["F_input_resolution"]["valid"]
    assert rep["I_poi_evidence"]["dataset_checks"] == {"facilities": "VERIFIED", "places": "VERIFIED"}
    j = rep["J_base_flow"]
    assert j["configured"]["status"] == "READY" and j["configured"]["config_status"] == "sourced"
    assert j["modelled_glofas_candidate"]["status"] == "CANDIDATE" and j["modelled_glofas_candidate"]["dataset_check"] == "VERIFIED"
    assert rep["E_hydrology"]["discharge_glofas"]["modelled_not_gauged"] is True

    # 6. nothing wrote to the site config; Teesta untouched --------------------------------------------------
    assert cfg_path.read_bytes() == cfg_bytes
    assert _snapshot(REPO / "sites" / "teesta.yaml", REPO / "config" / "input_resolution",
                     REPO / "docs" / "teesta_input_resolution.md") == teesta_before

    # 7. Teesta-leak audit over everything written for the synthetic site (+ report + job log) ---------
    corpus = {"<data_foundation report>": json.dumps(rep, default=str), "<markdown>": df.to_markdown(rep),
              "<job status>": json.dumps(status), "<register>": reg_bytes.decode()}
    for p in sorted(site.rglob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
            corpus[str(p.relative_to(data_dir))] = p.read_text(encoding="utf-8", errors="ignore")
    assert len(corpus) > 15  # terrain, breach, design, case files, evidence, provenance
    coords = _teesta_coordinate_strings()
    # src_048 is the GLOBAL OpenStreetMap source id (docs/data_sources.md: extracts live at
    # data/<site_id>/exposure/), cited only as the POI evidence's dataset source -- not Teesta provenance.
    osm = (REPO / "docs" / "data_sources.md").read_text(encoding="utf-8")
    assert "## src_048 — OpenStreetMap exposure extract" in osm and "data/<site_id>/exposure/" in osm
    allowed = {("synth_river_alpha/exposure/poi_evidence.json", "src_048")}
    leaks = [(name, w) for name, text in corpus.items() for w in LEAK_WORDS
             if w in text.lower() and (name, w) not in allowed]
    leaks += [(name, c) for name, text in corpus.items() for c in coords if re.search(rf"(?<![\d.]){re.escape(c)}(?!\d)", text)]
    assert leaks == [], leaks


def test_teesta_behaviour_unchanged(tmp_path):
    before = _snapshot(REPO / "sites" / "teesta.yaml", REPO / "config" / "input_resolution")
    rep = df.build("teesta", data_dir=tmp_path)
    assert rep["G_blocking"]["gate_verdict"] == "BLOCKED"
    assert rep["F_input_resolution"]["counts"] == {"APPROVED": 0, "CANDIDATE": 11, "REJECTED": 0, "UNRESOLVED": 31}  # after Feature 15
    assert rinit.init_register("teesta")["status"] == "exists"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        cfg = load_site_config("teesta")
    assert bfc.run(cfg, data_dir=tmp_path)["status"] == "UNRESOLVED"
    assert _snapshot(REPO / "sites" / "teesta.yaml", REPO / "config" / "input_resolution") == before

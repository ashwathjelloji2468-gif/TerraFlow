"""Feature 3: XZ9 peak discharge, peak-discharge ranges, per-dam placeholder flags, data dir,
reference hydrographs, Feature 2 evidence (docs/decisions.md 2026-10-01, F3-D1..D6)."""

from __future__ import annotations

import copy
import json
import math

import numpy as np
import pytest
import rasterio

from backend.m0_api import schemas
from backend.m2_breach import breach_params as bp
from backend.m2_breach import dfm, reference, xz9
from backend.m2_breach.hydrograph import hydrograph_for_dam, write_hydrograph
from backend.shared.site_config import SiteConfig
from tests.shared.conftest import fully_sourced

from .conftest import SYNTH_BREACH_HYDROGRAPH, SYNTH_VOLUME_ELEVATION_RELATION

ARGS = dict(V_w=1e6, h_w=10.0, h_b=5.0, h_d=15.0)


def _sourced_cfg(synth_raw, **dam0) -> SiteConfig:
    raw = fully_sourced(copy.deepcopy(synth_raw))
    raw["dams"][0].update(dam0)
    return SiteConfig.model_validate(raw)


def _set_dam_type(synth_raw, code: str) -> SiteConfig:
    raw = fully_sourced(copy.deepcopy(synth_raw))
    raw["dams"][0]["breach_inputs"]["dam_type"]["value"] = code
    return SiteConfig.model_validate(raw)


# --- XZ9 peak discharge ---------------------------------------------------------------------------
@pytest.mark.parametrize("dam_type, b3", [("CD", -0.503), ("FD", -0.591), ("HD", -0.649), ("ZD", -0.649)])
@pytest.mark.parametrize("mode, b4", [("O", -0.705), ("P", -1.039)])
@pytest.mark.parametrize("erod, b5", [("H", -0.007), ("M", -0.375), ("L", -1.362)])
def test_xz9_qp_every_coefficient_branch(dam_type, b3, mode, b4, erod, b5):
    r = xz9.peak_discharge_xz9(**ARGS, dam_type=dam_type, failure_mode=mode, erodibility=erod)
    expected = (0.175 * math.sqrt(9.81) * 1e6 ** (5 / 6) * (15.0 / 15.0) ** 0.199
                * ((1e6) ** (1 / 3) / 10.0) ** -1.274 * math.exp(b3 + b4 + b5))
    assert r.value == pytest.approx(expected, rel=1e-12)
    assert r.coefficients["b3_dam_type"] == b3 and r.coefficients["b4_failure_mode"] == b4
    assert r.coefficients["b5_erodibility"] == b5 and r.coefficients["g"] == 9.81


def test_xz9_qp_units_scale_as_m3_per_s():
    """sqrt(g)*V_w^(5/6) carries the m^3/s; (V_w^(1/3)/h_w) is dimensionless. Scaling every
    length in those two factors by k (V_w by k^3, h_w by k; h_d fixed against h_r) must scale
    Q_p by k^(5/2) exactly."""
    k = 2.0
    base = xz9.peak_discharge_xz9(**ARGS, dam_type="HD", failure_mode="O", erodibility="M").value
    scaled = xz9.peak_discharge_xz9(V_w=1e6 * k ** 3, h_w=10.0 * k, h_b=5.0, h_d=15.0,
                                    dam_type="HD", failure_mode="O", erodibility="M").value
    assert scaled / base == pytest.approx(k ** 2.5, rel=1e-12)


def test_xz9_qp_is_unverified_secondary_and_width_label_fixed():
    qp = xz9.peak_discharge_xz9(**ARGS, dam_type="HD", failure_mode="O", erodibility="M")
    bw = xz9.breach_width_xz9(**ARGS, dam_type="HD", failure_mode="O", erodibility="M")
    for r in (qp, bw):
        assert r.verified is False
        assert r.source_tag.startswith("SECONDARY")
    assert "PRIMARY" not in qp.source_tag
    assert bw.source_tag.startswith("SECONDARY (Azmi p.8, Table 3")


@pytest.mark.parametrize("override", [{"dam_type": "XX"}, {"failure_mode": "Q"}, {"erodibility": "Z"}, {"V_w": -1.0}, {"h_w": 0.0}])
def test_xz9_qp_rejects_invalid_inputs(override):
    kwargs = {**ARGS, "dam_type": "HD", "failure_mode": "O", "erodibility": "M", **override}
    with pytest.raises(ValueError):
        xz9.peak_discharge_xz9(**kwargs)


# --- fusion and ranges -----------------------------------------------------------------------------
def test_dfm_fusions_are_the_weighted_sums_of_real_methods(synth_raw):
    entry = bp.compute_dam(_sourced_cfg(synth_raw).dams[0])
    m = entry["parameters"]["peak_discharge_m3s"]["methods"]
    assert m["DFM_updated"]["value"] == pytest.approx(
        0.3048 * m["F16"]["value"] + 0.4804 * m["XZ9"]["value"] + 0.1674 * m["Z20"]["value"])
    assert m["DFM_2024"]["value"] == pytest.approx(
        1.23 * m["F16"]["value"] - 0.84 * m["H14"]["value"] + 0.26 * m["XZ9"]["value"])
    assert dfm._QP_COEF == {"F16": 0.3048, "XZ9": 0.4804, "Z20": 0.1674}  # unchanged


@pytest.mark.parametrize("code", ["HD", "CD"])
def test_peak_range_computable_for_hd_and_cd(synth_raw, code):
    qp = bp.compute_dam(_set_dam_type(synth_raw, code).dams[0])["parameters"]["peak_discharge_m3s"]
    assert "status" not in qp and 0 < qp["low"] <= qp["high"]
    assert qp["low"] == min(qp["methods"]["DFM_updated"]["value"], qp["methods"]["DFM_2024"]["value"])


@pytest.mark.parametrize("code", ["FD", "ZD"])
def test_peak_range_stays_blocked_for_fd_and_zd(synth_raw, code):
    entry = bp.compute_dam(_set_dam_type(synth_raw, code).dams[0])
    qp = entry["parameters"]["peak_discharge_m3s"]
    assert qp["status"] == "blocked" and qp["low"] is None and "Z20" in qp["reason"]
    assert qp["methods"]["XZ9"]["value"] > 0  # XZ9 itself still computes
    assert qp["methods"]["DFM_2024"]["value"] > 0  # no fallback pair is substituted
    assert "z20_dam_type_unmapped" in entry["warnings"]


# --- placeholder flags ------------------------------------------------------------------------------
def test_placeholder_valued_inputs_are_flagged_per_dam(synth_raw):
    raw = fully_sourced(copy.deepcopy(synth_raw))
    raw["dams"][0]["breach_inputs"]["dam_height"]["status"] = "placeholder"  # value kept
    entry = bp.compute_dam(SiteConfig.model_validate(raw).dams[0])
    assert "placeholder_data" in entry["warnings"]
    assert "placeholder-valued input(s) used: h_d" in entry["warnings"]


def test_fully_sourced_dam_has_no_placeholder_warning(synth_raw):
    entry = bp.compute_dam(_sourced_cfg(synth_raw).dams[0])
    assert "placeholder_data" not in entry["warnings"]


# --- data dir ------------------------------------------------------------------------------------------
def test_writers_respect_sih26_data_dir(tmp_path, synth_raw, monkeypatch):
    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "data"))
    cfg = _sourced_cfg(synth_raw)
    path = bp.write_breach_params(cfg)
    assert path == tmp_path / "data" / cfg.site.id / "breach" / "breach_params.json"
    hg = hydrograph_for_dam(cfg.dams[0], {"water_volume_m3": 1e6, "breach_width_m": 40.0,
                                          "failure_time_s": 1200.0, "peak_discharge_m3s": 800.0})
    csv_path, _ = write_hydrograph(hg, cfg.site.id, "synth_n_test")
    assert csv_path.parent == tmp_path / "data" / cfg.site.id / "breach" / "hydrographs"


# --- reference hydrographs -------------------------------------------------------------------------------
def _stage(cfg, tmp_path):
    events: list[str] = []
    path = reference.run_breach_stage(cfg, data_dir=tmp_path, event=events.append)
    payload = json.loads(path.read_text())
    schemas.validate("breach_params.schema.json", payload)
    return payload, events


def test_reference_triangular_hydrographs_use_range_bounds(tmp_path, synth_raw):
    cfg = _sourced_cfg(synth_raw)
    payload, events = _stage(cfg, tmp_path)
    dam_entry = payload["dams"][0]
    recs = {(r["dam_id"], r["case"]): r for r in payload["provenance"]["reference_hydrographs"]}
    low, high = recs[(cfg.dams[0].id, "low")], recs[(cfg.dams[0].id, "high")]
    for rec in (low, high):
        assert rec["status"] == "written" and rec["method"] == "triangular"
        sidecar = json.loads((tmp_path / cfg.site.id / rec["sidecar"]).read_text())
        schemas.validate("hydrograph_sidecar.schema.json", sidecar)
        assert abs(sidecar["mass_balance_error_pct"]) < 1e-6
    p = dam_entry["parameters"]
    assert low["params"]["breach_width_m"] == p["breach_width_m"]["low"]
    assert low["params"]["failure_time_s"] == p["failure_time_s"]["high"]
    assert low["params"]["peak_discharge_m3s"] == p["peak_discharge_m3s"]["low"]
    assert high["params"]["peak_discharge_m3s"] == p["peak_discharge_m3s"]["high"]
    assert high["peak_q_m3s"] == pytest.approx(p["peak_discharge_m3s"]["high"])
    assert any("hydrograph written" in e for e in events)


def test_reference_uses_weir_when_its_inputs_are_sourced(tmp_path, synth_raw):
    cfg = _sourced_cfg(synth_raw, volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    payload, _ = _stage(cfg, tmp_path)
    recs = [r for r in payload["provenance"]["reference_hydrographs"] if r["dam_id"] == cfg.dams[0].id]
    assert {r["method"] for r in recs} == {"breach_growth_weir"}


def test_fd_dam_reference_is_blocked_and_writes_nothing(tmp_path, synth_raw):
    cfg = _set_dam_type(synth_raw, "FD")
    payload, events = _stage(cfg, tmp_path)
    recs = [r for r in payload["provenance"]["reference_hydrographs"] if r["dam_id"] == cfg.dams[0].id]
    assert all(r["status"] == "blocked" and "peak_discharge_m3s range is blocked" in r["reason"] for r in recs)
    assert not list((tmp_path / cfg.site.id / "breach" / "hydrographs").glob(f"*__{cfg.dams[0].id}.*"))
    assert any("reference hydrograph blocked" in e for e in events)


def test_null_volume_blocks_reference(tmp_path, synth_raw):
    cfg = SiteConfig.model_validate(copy.deepcopy(synth_raw))  # dams[0] V_w is a null placeholder
    payload, _ = _stage(cfg, tmp_path)
    recs = [r for r in payload["provenance"]["reference_hydrographs"] if r["dam_id"] == cfg.dams[0].id]
    assert all(r["status"] == "blocked" and "water_volume_above_invert" in r["reason"] for r in recs)


def test_triggered_cascade_dam_gets_no_reference_hydrograph(tmp_path, synth_cascade_config):
    payload, _ = _stage(synth_cascade_config, tmp_path)
    triggered = {d.id for d in synth_cascade_config.dams if d.triggered_by}
    assert triggered
    for rec in payload["provenance"]["reference_hydrographs"]:
        if rec["dam_id"] in triggered:
            assert rec["status"] == "blocked" and "triggered cascade dam" in rec["reason"]


def test_infeasible_triangle_is_blocked_not_bent(tmp_path, synth_raw):
    raw = fully_sourced(copy.deepcopy(synth_raw))
    raw["dams"][0]["breach_inputs"]["water_volume_above_invert"]["value"] = 1e3  # tiny V: 2V/Qp < T_f
    payload, _ = _stage(SiteConfig.model_validate(raw), tmp_path)
    rec = next(r for r in payload["provenance"]["reference_hydrographs"] if r["case"] == "low")
    assert rec["status"] == "blocked" and "infeasible" in rec["reason"]


# --- evidence -------------------------------------------------------------------------------------------
def test_evidence_is_context_only_and_marks_missing_items(tmp_path, synth_raw):
    cfg = _sourced_cfg(synth_raw)
    payload, _ = _stage(cfg, tmp_path)
    ev = payload["provenance"]["evidence"]
    assert {k: v["status"] for k, v in ev.items() if k != "note"} == {
        "dem": "unavailable", "water_mask": "unavailable", "lake_latest": "unavailable",
        "hydrobasins_catchment": "unavailable", "discharge_glofas": "unavailable"}
    assert payload["dams"][0]["inputs_used"]["Vw_m3"] == cfg.dams[0].breach_inputs.water_volume_above_invert.value


def test_evidence_reads_feature2_outputs_without_changing_inputs(tmp_path, synth_raw):
    cfg = _sourced_cfg(synth_raw)
    site = tmp_path / cfg.site.id
    (site / "terrain").mkdir(parents=True)
    (site / "raw").mkdir()
    (site / "terrain" / "provenance.json").write_text(json.dumps(
        {"dem": {"product": "copernicus_glo30"}, "vertical_datum": "EGM2008", "dem_selection": {"threshold_met": True}}))
    (site / "terrain" / "grid.json").write_text("{}")
    profile = {"driver": "GTiff", "width": 4, "height": 4, "count": 1, "dtype": "uint8",
               "crs": "EPSG:32645", "transform": rasterio.transform.from_origin(0, 400, 100, 100)}
    with rasterio.open(site / "terrain" / "water_mask.tif", "w", **profile) as ds:
        ds.write(np.array([[[1, 1, 0, 0], [1, 0, 0, 0], [0, 0, 2, 2], [0, 0, 0, 0]]], dtype=np.uint8))
    (site / "raw" / "provenance.json").write_text(json.dumps({
        "discharge_glofas": {"status": "fetched", "kind": "modelled", "summary_m3s": {"median": 42.0},
                             "period": ["1984-01-01", "2024-12-31"], "point_basis": "x"},
        "hydrobasins_catchment": {"status": "unavailable", "reason": "HYDROBASINS_DIR is not set"}}))
    before = bp.compute_dam(cfg.dams[0])
    payload, _ = _stage(cfg, tmp_path)
    ev = payload["provenance"]["evidence"]
    assert ev["dem"]["product"] == "copernicus_glo30"
    assert ev["water_mask"]["lake_area_m2"] == pytest.approx(3 * 100 * 100)
    assert ev["water_mask"]["reservoir_area_m2"] == pytest.approx(2 * 100 * 100)
    assert ev["discharge_glofas"]["summary_m3s"]["median"] == 42.0
    assert ev["hydrobasins_catchment"]["reason"] == "HYDROBASINS_DIR is not set"
    assert payload["dams"][0]["parameters"] == before["parameters"]  # evidence changes no calculation

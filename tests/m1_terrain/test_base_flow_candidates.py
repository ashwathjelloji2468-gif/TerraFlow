"""Feature 16 P5: GloFAS base-flow evidence is a pending, MODELLED candidate with no proposed value.
Synthetic GloFAS fixtures and a synthetic non-Teesta site; no network."""
from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest
import yaml

from backend.m1_terrain import base_flow_candidates as bfc
from backend.m1_terrain import discharge
from backend.shared import data_foundation as df
from backend.shared.site_config import SiteConfig

REPO = Path(__file__).resolve().parents[2]
SID = "kosi_reach"
INFLOW = [86.95, 26.55]
SERIES = [3.0, 5.0, None, 4.0, 10.0, 2.0, 8.0]


def _raw_cfg(inflow_status="sourced", inflow=INFLOW, base_flow=None):
    raw = yaml.safe_load((REPO / "sites" / "synth_engdam.yaml").read_text(encoding="utf-8"))
    raw["site"]["id"] = SID
    raw["domains"]["far_field"]["inflow"]["location"] = {
        "value": inflow, "unit": "deg", "source": "synthetic surveyed point" if inflow_status == "sourced" else "",
        "status": inflow_status}
    if base_flow is not None:
        raw["domains"]["far_field"]["inflow"]["base_flow"] = base_flow
    return raw


def _cfg(**kw) -> SiteConfig:
    return SiteConfig.model_validate(_raw_cfg(**kw))


def _write_glofas(data_dir: Path, *, point=INFLOW, basis="domains.far_field.inflow.location", values=SERIES,
                  sha=None, summary=None, units="m^3/s", status="fetched"):
    raw = data_dir / SID / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    csv_path = raw / discharge.OUTPUT_NAME
    lines = ["date,river_discharge_m3s"] + [f"2020-01-{i + 1:02d},{'' if v is None else v}" for i, v in enumerate(values)]
    csv_path.write_text("\n".join(lines) + "\n")
    valid = [v for v in values if v is not None]
    entry = {"file": discharge.OUTPUT_NAME, "status": status, "dataset": discharge.DATASET, "source": discharge.SOURCE_ID,
             "service_url": discharge.SERVICE_URL, "kind": "modelled (GloFAS reanalysis / archived forecast), not gauged",
             "request_point_lonlat": point, "point_basis": basis, "returned_point_lonlat": [86.95, 26.55],
             "period": ["2020-01-01", "2020-01-07"], "n_days": len(values), "n_missing": len(values) - len(valid),
             "units": units, "time_basis": "daily, UTC dates", "license": discharge.LICENSE,
             "summary_m3s": summary or {"min": min(valid), "p10": discharge._percentile(valid, 0.1),
                                        "median": statistics.median(valid), "mean": statistics.fmean(valid),
                                        "p90": discharge._percentile(valid, 0.9), "max": max(valid)},
             "sha256": sha or hashlib.sha256(csv_path.read_bytes()).hexdigest(), "size_bytes": csv_path.stat().st_size,
             "fetched_at": "2026-10-01T00:00:00+00:00"}
    (raw / "provenance.json").write_text(json.dumps({"discharge_glofas": entry}, indent=2))
    return entry


def test_valid_series_gives_pending_modelled_candidate(tmp_path):
    entry = _write_glofas(tmp_path)
    rec = bfc.evaluate(_cfg(), data_dir=tmp_path)
    assert rec["status"] == "CANDIDATE" and rec["review_state"] == "pending" and rec["proposed_value"] is None
    assert rec["modelled_not_gauged"] is True and rec["target"] == "domains.far_field.inflow.base_flow"
    assert rec["unit"] == "m^3/s" and set(rec["statistics_m3s"]) == {"min", "p10", "median", "mean", "p90", "max"}
    assert rec["statistics_m3s"]["median"] == 4.5 and rec["statistics_m3s"]["min"] == 2.0  # missing day excluded
    assert rec["statistic_definitions"]["p10"].startswith("10th percentile")
    ev = rec["evidence"]
    assert ev["sha256"] == entry["sha256"] and ev["file_check"] == "VERIFIED" and ev["file"] == "raw/discharge_glofas.csv"
    for k in ("dataset", "source", "period", "n_missing", "request_point_lonlat", "returned_point_lonlat", "fetched_at", "kind"):
        assert ev[k] == entry[k], k
    assert any("not gauged" in c for c in rec["caveats"])


def test_never_labelled_observed(tmp_path):
    _write_glofas(tmp_path)
    rec = bfc.evaluate(_cfg(), data_dir=tmp_path)
    text = json.dumps({k: v for k, v in rec.items() if k != "caveats"}).lower()
    for word in ("observed", "measured", "authoritative"):
        assert word not in text, word
    assert "approv" not in json.dumps(rec).lower().replace("approved_by", "")


def test_missing_glofas_is_unverifiable(tmp_path):
    assert bfc.evaluate(_cfg(), data_dir=tmp_path)["status"] == "UNVERIFIABLE"
    (tmp_path / SID / "raw").mkdir(parents=True)
    (tmp_path / SID / "raw" / "provenance.json").write_text(json.dumps(
        {"discharge_glofas": {"status": "unavailable", "reason": "network ingestion is disabled"}}))
    r = bfc.evaluate(_cfg(), data_dir=tmp_path)
    assert r["status"] == "UNVERIFIABLE" and "network ingestion is disabled" in r["reason"]


def test_csv_missing_is_unverifiable(tmp_path):
    _write_glofas(tmp_path)
    (tmp_path / SID / "raw" / discharge.OUTPUT_NAME).unlink()
    assert bfc.evaluate(_cfg(), data_dir=tmp_path)["status"] == "UNVERIFIABLE"


def test_wrong_units_unverifiable(tmp_path):
    _write_glofas(tmp_path, units="ft^3/s")
    assert bfc.evaluate(_cfg(), data_dir=tmp_path)["status"] == "UNVERIFIABLE"


def test_bad_hash_is_mismatch(tmp_path):
    _write_glofas(tmp_path, sha="0" * 64)
    r = bfc.evaluate(_cfg(), data_dir=tmp_path)
    assert r["status"] == "MISMATCH" and "sha256" in r["reason"]


def test_tampered_statistics_is_mismatch(tmp_path):
    _write_glofas(tmp_path, summary={"min": 2.0, "p10": 2.5, "median": 99.0, "mean": 5.33, "p90": 9.0, "max": 10.0})
    assert bfc.evaluate(_cfg(), data_dir=tmp_path)["status"] == "MISMATCH"


def test_series_requested_elsewhere_is_mismatch(tmp_path):
    _write_glofas(tmp_path, point=[86.0, 26.0])
    assert bfc.evaluate(_cfg(), data_dir=tmp_path)["status"] == "MISMATCH"


def test_missing_inflow_unresolved(tmp_path):
    _write_glofas(tmp_path)
    r = bfc.evaluate(_cfg(inflow_status="placeholder", inflow=None), data_dir=tmp_path)
    assert r["status"] == "UNRESOLVED" and "missing" in r["reason"]


def test_placeholder_inflow_unresolved(tmp_path):
    _write_glofas(tmp_path)
    r = bfc.evaluate(_cfg(inflow_status="placeholder"), data_dir=tmp_path)
    assert r["status"] == "UNRESOLVED" and "placeholder" in r["reason"]


def test_dam_fallback_sample_unresolved(tmp_path):
    _write_glofas(tmp_path, basis="dams[0] (main_dam).location")
    r = bfc.evaluate(_cfg(), data_dir=tmp_path)
    assert r["status"] == "UNRESOLVED" and "not at domains.far_field.inflow.location" in r["reason"]


def test_non_candidate_outcomes_write_nothing(tmp_path):
    _write_glofas(tmp_path, sha="0" * 64)
    out = bfc.run(_cfg(), data_dir=tmp_path)
    assert out["written"] is False and not (tmp_path / SID / "raw" / bfc.OUTPUT_NAME).exists()
    with pytest.raises(ValueError):
        bfc.write(out["record"], data_dir=tmp_path)


def test_existing_base_flow_preserved_and_config_untouched(tmp_path):
    configured = {"value": 12.5, "unit": "m^3/s", "source": "team decision record (synthetic)", "status": "sourced"}
    _write_glofas(tmp_path)
    sites = tmp_path / "sites"
    sites.mkdir()
    path = sites / f"{SID}.yaml"
    path.write_text(yaml.safe_dump(_raw_cfg(base_flow=configured), sort_keys=False))
    before = path.read_bytes()
    out = bfc.run(_cfg(base_flow=configured), data_dir=tmp_path)
    assert out["status"] == "CANDIDATE" and out["record"]["configured_base_flow"] == configured
    assert out["record"]["proposed_value"] is None
    assert path.read_bytes() == before


def test_deterministic_and_idempotent(tmp_path):
    _write_glofas(tmp_path)
    a = bfc.run(_cfg(), data_dir=tmp_path)
    text = (tmp_path / SID / "raw" / bfc.OUTPUT_NAME).read_bytes()
    b = bfc.run(_cfg(), data_dir=tmp_path)
    assert a["written"] is True and b["written"] is False
    assert (tmp_path / SID / "raw" / bfc.OUTPUT_NAME).read_bytes() == text
    assert str(tmp_path) not in text.decode() and "generated_at" not in text.decode()


def test_no_teesta_values(tmp_path):
    _write_glofas(tmp_path)
    bfc.run(_cfg(), data_dir=tmp_path)
    text = (tmp_path / SID / "raw" / bfc.OUTPUT_NAME).read_text().lower()
    for word in ("teesta", "lhonak", "chungthang", "src_043", "src_048"):
        assert word not in text, word


@pytest.mark.parametrize("bad", ["../x", "Teesta", "a/b", None])
def test_unsafe_site_id(tmp_path, bad):
    with pytest.raises(bfc.UnsafeEvidencePath):
        bfc.site_paths(bad, tmp_path)


def test_symlinked_raw_dir_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / SID).mkdir()
    (tmp_path / SID / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(bfc.UnsafeEvidencePath):
        bfc.evaluate(_cfg(), data_dir=tmp_path)


def test_teesta_is_unresolved_without_writing(tmp_path):
    import warnings

    from backend.shared.site_config import PlaceholderWarning, load_site_config
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        cfg = load_site_config("teesta")
    out = bfc.run(cfg, data_dir=tmp_path)
    assert out["status"] == "UNRESOLVED" and out["written"] is False  # inflow location is a placeholder


def test_data_foundation_separates_configured_from_candidate(tmp_path):
    configured = {"value": 12.5, "unit": "m^3/s", "source": "team decision record (synthetic)", "status": "sourced"}
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / f"{SID}.yaml").write_text(yaml.safe_dump(_raw_cfg(base_flow=configured), sort_keys=False))
    _write_glofas(tmp_path)
    kw = dict(data_dir=tmp_path, sites_dir=sites, register_dir=tmp_path / "reg", checklist_path=False)
    rep = df.build(SID, **kw)
    j = rep["J_base_flow"]
    assert j["configured"]["status"] == "READY" and j["modelled_glofas_candidate"]["file"] == "MISSING"
    bfc.run(_cfg(base_flow=configured), data_dir=tmp_path)
    j = df.build(SID, **kw)["J_base_flow"]
    c = j["modelled_glofas_candidate"]
    assert c["status"] == "CANDIDATE" and c["review_state"] == "pending" and c["proposed_value"] is None
    assert c["modelled_not_gauged"] is True and c["dataset_check"] == "VERIFIED"
    assert j["configured"]["config_status"] == "sourced"  # configured value untouched by the candidate
    (tmp_path / SID / "raw" / discharge.OUTPUT_NAME).write_text("date,river_discharge_m3s\n")
    rep = df.build(SID, **kw)
    assert rep["J_base_flow"]["modelled_glofas_candidate"]["dataset_check"] == "MISMATCH"
    assert any("base-flow GloFAS evidence" in w for w in rep["H_warnings"])


def test_cli(tmp_path, capsys, monkeypatch):
    _write_glofas(tmp_path)
    monkeypatch.setattr("backend.m3_dflowfm.preflight._load_config", lambda sid, d, s: (_cfg(), None))
    assert bfc.main([SID, "--data-dir", str(tmp_path)]) == 0
    first = capsys.readouterr().out
    assert bfc.main([SID, "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == first and not (tmp_path / SID / "raw" / bfc.OUTPUT_NAME).exists()
    assert bfc.main(["../x", "--data-dir", str(tmp_path)]) == 2

"""Feature 4: scenario design from Feature 3's breach_params.json, hydrograph-validated scenarios,
fingerprints/history/ID stability, emulator_inputs volume ranges (docs/decisions.md 2026-10-01)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import warnings

import pytest
import yaml

from backend.m0_api import schemas
from backend.m2_breach.breach_params import write_breach_params
from backend.m2_breach.hydrograph import hydrograph_for_dam
from backend.m5_emulator import scenario_design as sd
from backend.shared.site_config import SiteConfig
from tests.m2_breach.conftest import SYNTH_BREACH_HYDROGRAPH, SYNTH_VOLUME_ELEVATION_RELATION
from tests.shared.conftest import SYNTH_PATH, fully_sourced

SETTINGS = sd.ScenarioDesignSettings(n=8, n_holdout=2, seed=7)
ID_RE = re.compile(r"^synth__(s\d{3}|n_holdout\d{3}|n_[a-z][a-z0-9_]*)$")


def _raw(**dam0) -> dict:
    raw = fully_sourced(copy.deepcopy(yaml.safe_load(SYNTH_PATH.read_text())))
    raw["dams"][0].update(dam0)
    return raw


def _cfg(raw: dict) -> SiteConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return SiteConfig.model_validate(raw)


def _prepare(tmp_path, raw: dict | None = None) -> tuple[SiteConfig, object]:
    cfg = _cfg(raw or _raw())
    data = tmp_path / "data"
    write_breach_params(cfg, data_dir=data)
    return cfg, data


# --- peak discharge sampling ---------------------------------------------------------------------
def test_peak_discharge_sampled_when_triangular_is_required(tmp_path):
    cfg, data = _prepare(tmp_path)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    qp = next(i for i in d["inputs"] if i["name"] == "peak_discharge_m3s")
    raw_lo, raw_hi = d["provenance"]["raw_m2_ranges"]["peak_discharge_m3s"]
    assert qp["low"] < raw_lo and qp["high"] > raw_hi  # widened like the other M2 ranges
    assert d["provenance"]["hydrograph_method"] == "triangular"
    for s in d["scenarios"] + d["extra"]:
        assert qp["low"] <= s["params"]["peak_discharge_m3s"] <= qp["high"]
    assert "peak_discharge_sampled_independently" in d["caveats"]
    schemas.validate("scenario_design.schema.json", d)


def test_peak_discharge_not_sampled_when_weir_method_is_sourced(tmp_path):
    cfg, data = _prepare(tmp_path, _raw(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                                        breach_hydrograph=SYNTH_BREACH_HYDROGRAPH))
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    assert d["provenance"]["hydrograph_method"] == "breach_growth_weir"
    assert all("peak_discharge_m3s" not in s["params"] for s in d["scenarios"] + d["extra"])
    assert {i["name"] for i in d["inputs"]} == {"breach_width_m", "failure_time_s"}


@pytest.mark.parametrize("code", ["FD", "ZD"])
def test_fd_zd_target_without_weir_is_blocked(tmp_path, code):
    raw = _raw()
    raw["dams"][0]["breach_inputs"]["dam_type"]["value"] = code
    cfg, data = _prepare(tmp_path, raw)
    with pytest.raises(sd.ScenarioDesignBlockedError) as e:
        sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    assert e.value.dam_id == "synth_lake" and e.value.param == "peak_discharge_m3s"
    assert "Z20" in e.value.reason


# --- every scenario has a real hydrograph -----------------------------------------------------------
def test_every_scenario_produces_a_real_hydrograph_without_monkeypatching(tmp_path):
    cfg, data = _prepare(tmp_path)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    dam = cfg.dams[0]
    checks = {c["scenario_id"]: c for c in d["provenance"]["scenario_checks"]}
    for s in d["scenarios"] + d["extra"]:
        hg = hydrograph_for_dam(dam, s["params"])
        assert hg.method == "triangular" and hg.peak_q_m3s > 0
        assert checks[s["scenario_id"]]["peak_q_m3s"] == pytest.approx(hg.peak_q_m3s)


def test_infeasible_points_are_rejected_with_reason_not_repaired(tmp_path):
    raw = _raw()
    raw["dams"][0]["breach_inputs"]["water_volume_above_invert"]["value"] = 2.0e5  # small V: some 2V/Qp <= T_f
    cfg, data = _prepare(tmp_path, raw)
    try:
        d = sd.build_scenario_design(cfg, "synth_lake", sd.ScenarioDesignSettings(n=12, n_holdout=0, seed=3), data)
    except sd.ScenarioDesignBlockedError as e:  # every point infeasible is also an explicit outcome
        assert "produced a hydrograph" in e.reason
        return
    for r in d["provenance"]["rejected"]:
        assert r["dam_id"] == "synth_lake" and r["reason"]
    ids = [s["scenario_id"] for s in d["scenarios"]]
    assert ids == [f"synth__s{i:03d}" for i in range(1, len(ids) + 1)]  # IDs only for accepted points


# --- water volume --------------------------------------------------------------------------------------
def test_volume_fixed_without_emulator_inputs_range(tmp_path):
    cfg, data = _prepare(tmp_path)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    v = cfg.dams[0].breach_inputs.water_volume_above_invert.value
    assert {s["params"]["water_volume_m3"] for s in d["scenarios"] + d["extra"]} == {v}
    assert d["provenance"]["fixed_inputs"]["water_volume_m3"]["status"] == "fixed"
    assert "water_volume_m3" not in d["provenance"]["sampled_inputs"]


def test_volume_sampled_log_scaled_over_explicit_range(tmp_path):
    raw = _raw()
    raw["emulator_inputs"] = [{"name": "water_volume_m3", "dam_id": "synth_lake",
                               "range": {"low": 5.0e5, "high": 2.0e6, "unit": "m^3", "source": "synthetic test range",
                                         "status": "placeholder", "basis": "test"}}]
    cfg, data = _prepare(tmp_path, raw)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    vols = [s["params"]["water_volume_m3"] for s in d["scenarios"]]
    assert all(5.0e5 <= v <= 2.0e6 for v in vols) and len(set(vols)) == len(vols)
    assert d["provenance"]["water_volume"]["status"] == "sampled"
    assert d["provenance"]["scaling"]["water_volume_m3"] == "log10"
    vol_input = next(i for i in d["inputs"] if i["name"] == "water_volume_m3")
    assert (vol_input["low"], vol_input["high"]) == (5.0e5, 2.0e6)  # not widened


def test_emulator_inputs_validation():
    raw = _raw()
    raw["emulator_inputs"] = [{"name": "water_volume_m3", "dam_id": "nope",
                               "range": {"low": 1.0, "high": 2.0, "unit": "m^3", "source": "x"}}]
    with pytest.raises(Exception, match="not a dam"):
        _cfg(raw)
    raw["emulator_inputs"][0].update(dam_id="synth_lake", range={"low": 1.0, "high": 2.0, "unit": "m", "source": "x"})
    with pytest.raises(Exception, match="unit must be"):
        _cfg(raw)


# --- determinism, fingerprints, history, ID stability -------------------------------------------------
def _strip_time(d: dict) -> dict:
    d = copy.deepcopy(d)
    d["provenance"].pop("generated_at")
    return d


def test_design_is_deterministic(tmp_path):
    cfg, data = _prepare(tmp_path)
    a = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    b = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    assert _strip_time(a) == _strip_time(b)


@pytest.mark.parametrize("change", ["seed", "range"])
def test_changed_seed_or_range_changes_fingerprint(tmp_path, change):
    cfg, data = _prepare(tmp_path)
    base = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)["provenance"]["fingerprint"]
    if change == "seed":
        other = sd.build_scenario_design(cfg, "synth_lake", sd.ScenarioDesignSettings(n=8, n_holdout=2, seed=8), data)
    else:
        raw = _raw()
        raw["dams"][0]["breach_inputs"]["dam_height"]["value"] *= 1.5
        cfg2, data2 = _prepare(tmp_path / "b", raw)
        other = sd.build_scenario_design(cfg2, "synth_lake", SETTINGS, data2)
    assert other["provenance"]["fingerprint"] != base


def test_identical_regeneration_is_a_noop_and_change_archives_history(tmp_path):
    cfg, data = _prepare(tmp_path)
    path = sd.write_scenario_design(cfg, "synth_lake", data, SETTINGS)
    first = path.read_bytes()
    sd.write_scenario_design(cfg, "synth_lake", data, SETTINGS)
    assert path.read_bytes() == first  # no-op, timestamp kept
    assert not (path.parent / "history").exists()

    sd.write_scenario_design(cfg, "synth_lake", data, sd.ScenarioDesignSettings(n=8, n_holdout=2, seed=99))
    old = json.loads(first)
    archived = path.parent / "history" / f"{old['provenance']['fingerprint']}.json"
    assert json.loads(archived.read_text()) == old
    new = json.loads(path.read_text())
    old_ids = {s["scenario_id"]: s["params"] for s in old["scenarios"] + old["extra"]}
    for s in new["scenarios"] + new["extra"]:
        assert s["scenario_id"] not in old_ids  # never reused for different parameters
    assert new["scenarios"][0]["scenario_id"] == f"synth__s{len(old['scenarios']) + 1:03d}"
    for s in new["scenarios"] + new["extra"]:
        assert ID_RE.match(s["scenario_id"])


def test_breach_params_sha256_is_recorded(tmp_path):
    cfg, data = _prepare(tmp_path)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    file = data / "synth" / "breach" / "breach_params.json"
    assert d["provenance"]["breach_params_sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    for key in ("raw_m2_ranges", "widened_ranges", "input_widen_fraction", "method", "seed", "n_requested",
                "fixed_inputs", "sampled_inputs", "generated_at", "fingerprint", "placeholder_fields"):
        assert key in d["provenance"]


def test_missing_breach_params_is_explicit(tmp_path):
    cfg = _cfg(_raw())
    with pytest.raises(sd.BreachParamsUnavailable, match="run the breach stage"):
        sd.build_scenario_design(cfg, "synth_lake", SETTINGS, tmp_path / "empty")


def test_stale_breach_params_is_explicit(tmp_path):
    cfg, data = _prepare(tmp_path)
    raw = _raw()
    raw["dams"][0]["breach_inputs"]["water_height_above_invert"]["value"] += 1.0
    with pytest.raises(sd.BreachParamsUnavailable, match="stale"):
        sd.build_scenario_design(_cfg(raw), "synth_lake", SETTINGS, data)


def test_design_ranges_come_from_the_file_not_recomputed(tmp_path):
    cfg, data = _prepare(tmp_path)
    path = data / "synth" / "breach" / "breach_params.json"
    payload = json.loads(path.read_text())
    payload["dams"][0]["parameters"]["breach_width_m"].update(low=10.0, high=20.0)
    path.write_text(json.dumps(payload))
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    assert d["provenance"]["raw_m2_ranges"]["breach_width_m"] == [10.0, 20.0]


def test_id_format_matches_the_code_wide_scheme(tmp_path):
    cfg, data = _prepare(tmp_path)
    d = sd.build_scenario_design(cfg, "synth_lake", SETTINGS, data)
    assert all(ID_RE.match(s["scenario_id"]) for s in d["scenarios"] + d["extra"])
    from backend.m2_breach.reference import reference_scenario_id

    assert reference_scenario_id("synth", "low") == "synth__n_m2_low"

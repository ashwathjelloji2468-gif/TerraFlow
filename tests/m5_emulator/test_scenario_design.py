"""Tests for backend.m5_emulator.scenario_design (docs/handoff_contract.md
§4.3, docs/m5_specs.md §2).

`design_from_ranges` is tested directly against synthetic ranges, and
`build_scenario_design` exercises the real M2 breach-width range.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest
import yaml

from backend.m0_api import schemas
from backend.m5_emulator.scenario_design import (
    RANGED_INPUTS,
    ScenarioDesignBlockedError,
    ScenarioDesignSettings,
    build_scenario_design,
    design_from_ranges,
    load_scenario_design_settings,
    write_scenario_design,
)

SYNTH_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "shared" / "synth.yaml"

RANGES = {"breach_width_m": (40.0, 120.0), "failure_time_s": (600.0, 7200.0)}
SETTINGS = ScenarioDesignSettings(n=8, n_holdout=3, seed=42, input_widen_fraction=0.20)


def _fully_sourced_synth_raw() -> dict:
    with open(SYNTH_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = copy.deepcopy(data)
    data["domains"]["near_field"]["inflow"]["location"] = {
        "value": [88.485, 27.499], "unit": "deg", "source": "test", "status": "sourced",
    }
    data["dams"][0]["breach_inputs"]["water_volume_above_invert"] = {
        "value": 1_000_000, "unit": "m^3", "source": "test", "status": "sourced",
    }
    data["points_of_interest"][1]["location"] = {
        "value": [88.52, 27.47], "unit": "deg", "source": "test", "status": "sourced",
    }
    data["events"][0]["imagery_post_event"] = {
        "value": "2020-01-05", "unit": "iso8601", "source": "test", "status": "sourced",
    }
    return data


def _load(raw: dict, tmp_path: Path):
    from backend.shared.site_config import load_site_config

    path = tmp_path / f"{raw['site']['id']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_site_config(path)


# --------------------------------------------------------------------------- design_from_ranges (pure)


def test_design_from_ranges_scenario_count_and_ids():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    assert len(payload["scenarios"]) == SETTINGS.n
    assert len(payload["extra"]) == SETTINGS.n_holdout
    assert [s["scenario_id"] for s in payload["scenarios"][:2]] == ["synth__s001", "synth__s002"]
    assert payload["extra"][0]["scenario_id"] == "synth__n_holdout001"
    assert all(s["kind"] == "design" for s in payload["scenarios"])
    assert all(s["kind"] == "held_out" for s in payload["extra"])


def test_design_from_ranges_water_volume_fixed_across_scenarios():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    volumes = {s["params"]["water_volume_m3"] for s in payload["scenarios"] + payload["extra"]}
    assert volumes == {1_000_000.0}


def test_design_from_ranges_widened_inputs_bracket_raw_range():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    by_name = {i["name"]: i for i in payload["inputs"]}
    for name, (lo, hi) in RANGES.items():
        assert by_name[name]["low"] < lo
        assert by_name[name]["high"] > hi
        assert by_name[name]["dam_id"] == "synth_lake"


def test_design_from_ranges_scenario_params_within_widened_box():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    by_name = {i["name"]: i for i in payload["inputs"]}
    for s in payload["scenarios"] + payload["extra"]:
        for name in RANGED_INPUTS:
            lo, hi = by_name[name]["low"], by_name[name]["high"]
            assert lo <= s["params"][name] <= hi


def test_design_from_ranges_is_deterministic_given_seed():
    a = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    b = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], SETTINGS)
    assert a["scenarios"] == b["scenarios"]
    assert a["extra"] == b["extra"]


def test_design_from_ranges_zero_holdout_gives_empty_extra():
    settings = ScenarioDesignSettings(n=5, n_holdout=0, seed=1)
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES, [], settings)
    assert payload["extra"] == []


def test_design_from_ranges_validates_against_schema():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES,
                                  ["moraine_extrapolation"], SETTINGS)
    schemas.validate("scenario_design.schema.json", payload)


def test_design_from_ranges_caveats_passed_through():
    payload = design_from_ranges("synth", "synth_lake", 1_000_000.0, RANGES,
                                  ["moraine_extrapolation"], SETTINGS)
    assert payload["caveats"] == ["moraine_extrapolation"]
    assert payload["has_placeholders"] is True


# --------------------------------------------------------------------------- build_scenario_design (real M2)


def test_build_scenario_design_uses_computed_breach_width_range(tmp_path):
    """Feature 4: the design reads Feature 3's breach_params.json (written here first)."""
    from backend.m2_breach.breach_params import write_breach_params

    cfg = _load(_fully_sourced_synth_raw(), tmp_path)
    write_breach_params(cfg, data_dir=tmp_path / "data")
    payload = build_scenario_design(cfg, "synth_lake", SETTINGS, data_dir=tmp_path / "data")
    width = next(item for item in payload["inputs"] if item["name"] == "breach_width_m")
    assert width["low"] > 0
    assert width["high"] > width["low"]
    assert payload["scenarios"]
    assert all(item["params"]["breach_width_m"] > 0 for item in payload["scenarios"])


def test_build_scenario_design_blocked_on_placeholder_water_volume(tmp_path):
    raw = _fully_sourced_synth_raw()
    cfg = _load(raw, tmp_path)
    assert cfg.dams[0].breach_inputs.water_volume_above_invert.value == 1_000_000

    raw["dams"][0]["breach_inputs"]["water_volume_above_invert"] = {
        "value": None, "unit": "m^3", "source": "", "status": "placeholder",
    }
    cfg2 = _load(raw, tmp_path)
    with pytest.raises(ScenarioDesignBlockedError) as exc_info:
        build_scenario_design(cfg2, "synth_lake", SETTINGS)
    assert exc_info.value.param == "water_volume_m3"


def test_build_scenario_design_unknown_dam_raises(tmp_path):
    cfg = _load(_fully_sourced_synth_raw(), tmp_path)
    with pytest.raises(ValueError, match="no dam"):
        build_scenario_design(cfg, "no_such_dam", SETTINGS)


def test_write_scenario_design_propagates_block(tmp_path):
    raw = _fully_sourced_synth_raw()
    raw["dams"][0]["breach_inputs"]["water_volume_above_invert"] = {
        "value": None, "unit": "m^3", "source": "", "status": "placeholder",
    }
    cfg = _load(raw, tmp_path)
    with pytest.raises(ScenarioDesignBlockedError):
        write_scenario_design(cfg, "synth_lake", data_dir=tmp_path / "data", settings=SETTINGS)
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------- settings loader


def test_load_scenario_design_settings_reads_real_config():
    settings = load_scenario_design_settings()
    assert settings.n == 30
    assert settings.n_holdout == 5
    assert settings.seed == 42
    assert settings.input_widen_fraction == 0.20
    assert settings.method == "maximin_lhs"


def test_load_scenario_design_settings_overrides(tmp_path):
    path = tmp_path / "m5_scenario_design.yaml"
    path.write_text("n: 10\nseed: 7\n", encoding="utf-8")
    settings = load_scenario_design_settings(path)
    assert settings.n == 10
    assert settings.seed == 7
    assert settings.n_holdout == 5  # dataclass default, not overridden


def test_load_scenario_design_settings_unknown_key_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("not_a_real_setting: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown setting"):
        load_scenario_design_settings(path)


def test_scenario_design_settings_rejects_bad_values():
    with pytest.raises(ValueError):
        ScenarioDesignSettings(n=0)
    with pytest.raises(ValueError):
        ScenarioDesignSettings(n_holdout=-1)
    with pytest.raises(ValueError):
        ScenarioDesignSettings(input_widen_fraction=1.0)
    with pytest.raises(ValueError):
        ScenarioDesignSettings(method="something_else")

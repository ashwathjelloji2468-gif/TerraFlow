"""Tests for backend.m2_breach.breach_params — per-dam and per-site orchestration."""

from __future__ import annotations

import copy
import warnings

import pydantic
import pytest

from backend.m2_breach.breach_params import compute_breach_params, compute_dam
from backend.m0_api import schemas
from backend.shared.site_config import PlaceholderWarning, SiteConfig, load_site_config
from tests.m2_breach.conftest import SYNTH_IMPOSED_RANGES


def test_fully_sourced_hd_moraine_dam(make_config):
    """dams[0] in synth.yaml: moraine_dammed_lake, HD, all inputs present."""
    cfg = make_config()
    entry = compute_dam(cfg.dams[0])

    assert entry["equations_applicable"] is True
    params = entry["parameters"]

    # Feature 3 (F3-D1): XZ9 peak discharge is enabled, so an HD dam's Qp pair is computable.
    qp = params["peak_discharge_m3s"]
    assert qp["low"] is not None and qp["high"] is not None and qp["low"] <= qp["high"]
    assert "status" not in qp
    assert qp["selected_pair"] == ["DFM_updated", "DFM_2024"]
    assert qp["methods"]["XZ9"]["value"] > 0
    assert params["breach_width_m"]["low"] is not None
    assert params["breach_width_m"]["high"] is not None
    assert "status" not in params["breach_width_m"]

    tf = params["failure_time_s"]
    assert tf["low"] is not None and tf["high"] is not None
    assert tf["low"] <= tf["high"]
    assert tf["unit"] == "s"
    assert tf["selected_pair"] == ["DFM_updated", "F8"]

    # F16 and Z20 (HD) are individually computable too.
    assert params["peak_discharge_m3s"]["methods"]["F16"]["value"] is not None
    assert params["peak_discharge_m3s"]["methods"]["Z20"]["value"] is not None
    assert params["breach_width_m"]["methods"]["XZ9"]["value"] > 0

    assert "moraine_extrapolation" in entry["warnings"]
    assert "failure_time_uncertain" in entry["warnings"]


def test_concrete_dam_without_equations_applicable_false_rejected_by_loader(make_config):
    """docs/Equations.md §7 refuses kind: concrete_dam; the loader (not compute_dam) now
    enforces that such a dam must set equations_applicable: false and provide imposed_ranges."""
    with pytest.raises(pydantic.ValidationError, match="equations_applicable"):
        make_config(kind="concrete_dam")


def test_concrete_dam_uses_imposed_ranges(make_config):
    cfg = make_config(kind="concrete_dam", equations_applicable=False, imposed_ranges=SYNTH_IMPOSED_RANGES)
    entry = compute_dam(cfg.dams[0])

    assert entry["equations_applicable"] is False
    assert "concrete_dam_imposed" in entry["warnings"]

    qp = entry["parameters"]["peak_discharge_m3s"]
    assert qp["interval"] == "imposed"
    assert qp["low"] == 800.0 and qp["high"] == 1500.0
    assert qp["selected_pair"] is None
    assert "status" not in qp


def test_concrete_dam_imposed_range_placeholder_blocks(make_config):
    ranges = copy.deepcopy(SYNTH_IMPOSED_RANGES)
    ranges["peak_discharge_m3s"] = {"value": None, "unit": "m^3/s",
                                     "source": "Placeholder: not sourced yet", "status": "placeholder"}
    cfg = make_config(kind="concrete_dam", equations_applicable=False, imposed_ranges=ranges)
    entry = compute_dam(cfg.dams[0])

    qp = entry["parameters"]["peak_discharge_m3s"]
    assert qp["low"] is None and qp["status"] == "blocked"
    assert "placeholder_data" in entry["warnings"]


def test_z20_blocked_for_fd_dam_type(make_config):
    cfg = make_config(dam_type="FD")
    entry = compute_dam(cfg.dams[0])
    assert entry["parameters"]["peak_discharge_m3s"]["methods"]["Z20"]["status"] == "blocked"
    assert "z20_dam_type_unmapped" in entry["warnings"]


def test_missing_input_blocks_dependent_methods(synth_raw):
    """dams[0] as shipped has water_volume_above_invert (V_w) as a null placeholder."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = SiteConfig.model_validate(synth_raw)

    entry = compute_dam(cfg.dams[0])
    params = entry["parameters"]
    # V_w feeds every method for every output.
    assert params["peak_discharge_m3s"]["methods"]["F16"]["status"] == "blocked"
    assert params["breach_width_m"]["methods"]["F95"]["status"] == "blocked"
    assert params["failure_time_s"]["methods"]["MCLM"]["status"] == "blocked"
    assert "placeholder_data" in entry["warnings"]


def test_breach_height_exceeds_dam_height_warns(make_config):
    cfg = make_config(breach_height=30, dam_height=25)
    entry = compute_dam(cfg.dams[0])
    assert any("exceeds dam_height" in w for w in entry["warnings"])


def test_compute_breach_params_site_level(synth_config_sourced):
    payload = compute_breach_params(synth_config_sourced)
    assert payload["site_id"] == "synth"
    assert payload["has_placeholders"] is False
    assert payload["placeholder_fields"] == []
    assert len(payload["dams"]) == 1
    assert payload["provenance"]["method"] == "empirical"
    schemas.validate("breach_params.schema.json", payload)


def test_compute_breach_params_site_level_with_placeholder(synth_config_with_placeholder):
    payload = compute_breach_params(synth_config_with_placeholder)
    assert payload["has_placeholders"] is True
    assert payload["placeholder_fields"] == ["dams[0].breach_inputs.water_volume_above_invert"]
    schemas.validate("breach_params.schema.json", payload)


def test_teesta_site_config_end_to_end():
    """The real (placeholder-heavy) teesta.yaml: two dams, one FD (teesta_iii,
    Z20 blocked), has_placeholders true throughout."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        cfg = load_site_config("teesta")

    payload = compute_breach_params(cfg)
    schemas.validate("breach_params.schema.json", payload)

    assert payload["has_placeholders"] is True
    assert len(payload["dams"]) == 2

    teesta_iii = next(d for d in payload["dams"] if d["dam_id"] == "teesta_iii")
    assert teesta_iii["parameters"]["peak_discharge_m3s"]["methods"]["Z20"]["status"] == "blocked"
    assert "z20_dam_type_unmapped" in teesta_iii["warnings"]

    south_lhonak = next(d for d in payload["dams"] if d["dam_id"] == "south_lhonak")
    assert "moraine_extrapolation" in south_lhonak["warnings"]


def test_write_breach_params(tmp_path, synth_config_sourced):
    from backend.m2_breach.breach_params import write_breach_params

    out_path = write_breach_params(synth_config_sourced, data_dir=tmp_path)
    assert out_path.is_file()
    assert out_path == tmp_path / "synth" / "breach" / "breach_params.json"

    import json
    payload = json.loads(out_path.read_text())
    schemas.validate("breach_params.schema.json", payload)

"""Tests for backend.m2_breach.hydrograph."""

from __future__ import annotations

import numpy as np
import pytest

from backend.m2_breach import hydrograph as hg_mod
from backend.m2_breach.hydrograph import (
    HydrographBlocked,
    breach_growth_weir,
    hydrograph_for_dam,
    triangular,
    write_hydrograph,
)
from backend.m2_breach.storage import from_area_volume_relation, from_surveyed_curve
from tests.m2_breach.conftest import (
    SYNTH_BREACH_HYDROGRAPH,
    SYNTH_VOLUME_ELEVATION_RELATION,
    _placeholder,
)

# --- breach_growth_weir -----------------------------------------------------


def _weir_curve():
    return from_area_volume_relation(V_w=1_000_000.0, h_w=15.0, b=1.5)


def test_weir_mass_conservation_area_volume_relation():
    curve = _weir_curve()
    hg = breach_growth_weir(curve, h0=15.0, B_ave=40.0, h_b=15.0, T_f=1200.0,
                             C_r=1.7, C_s=1.3, z=0.5, dt_s=10.0)
    assert abs(hg.mass_balance_error_pct) < 0.5
    assert hg.volume_m3 >= 0.995 * curve.volume(15.0)


def test_weir_mass_conservation_surveyed_curve():
    curve = from_surveyed_curve(
        [[2870.0, 0.0], [2875.0, 1_500_000.0], [2880.0, 4_000_000.0], [2890.0, 12_000_000.0]],
        invert_elevation_m=2870.0,
    )
    h0 = curve.head(4_000_000.0)
    hg = breach_growth_weir(curve, h0=h0, B_ave=60.0, h_b=10.0, T_f=1800.0,
                             C_r=1.7, C_s=1.3, z=0.3, dt_s=10.0)
    assert abs(hg.mass_balance_error_pct) < 0.5
    assert hg.volume_m3 >= 0.999 * curve.volume(h0)


def test_weir_zero_flow_before_breach_starts():
    curve = _weir_curve()
    hg = breach_growth_weir(curve, h0=15.0, B_ave=40.0, h_b=15.0, T_f=1200.0,
                             C_r=1.7, C_s=1.3, z=0.5, dt_s=10.0, t_offset_s=0.0)
    assert hg.q_m3s[0] == pytest.approx(0.0, abs=1e-9)


def test_weir_zero_flow_before_offset_start():
    curve = _weir_curve()
    hg = breach_growth_weir(curve, h0=15.0, B_ave=40.0, h_b=15.0, T_f=1200.0,
                             C_r=1.7, C_s=1.3, z=0.5, dt_s=10.0, t_offset_s=600.0)
    before = hg.q_m3s[hg.t_s <= 600.0]
    after_idx = np.searchsorted(hg.t_s, 600.0 + 60.0)
    assert np.all(before == 0.0)
    assert hg.q_m3s[after_idx] > 0.0


def test_weir_peak_discharge_is_positive_and_finite():
    curve = _weir_curve()
    hg = breach_growth_weir(curve, h0=15.0, B_ave=40.0, h_b=15.0, T_f=1200.0,
                             C_r=1.7, C_s=1.3, z=0.5, dt_s=10.0)
    assert hg.peak_q_m3s > 0.0
    assert np.isfinite(hg.peak_q_m3s)
    assert hg.time_to_peak_s >= 0.0


def test_weir_nonpositive_bottom_width_raises():
    curve = _weir_curve()
    with pytest.raises(ValueError):
        # B_ave - z*h_b <= 0
        breach_growth_weir(curve, h0=15.0, B_ave=5.0, h_b=15.0, T_f=1200.0,
                            C_r=1.7, C_s=1.3, z=0.5, dt_s=10.0)


# --- triangular --------------------------------------------------------------


def test_triangular_mass_conservation():
    hg = triangular(Q_p=500.0, V=1_000_000.0, T_f=1200.0, dt_s=10.0)
    assert hg.volume_m3 == pytest.approx(1_000_000.0, rel=1e-9)
    assert abs(hg.mass_balance_error_pct) < 1e-6


def test_triangular_peak_matches_input():
    hg = triangular(Q_p=500.0, V=1_000_000.0, T_f=1200.0, dt_s=10.0)
    assert hg.peak_q_m3s == pytest.approx(500.0)
    assert hg.time_to_peak_s == pytest.approx(1200.0)


def test_triangular_zero_flow_before_breach_starts():
    hg = triangular(Q_p=500.0, V=1_000_000.0, T_f=1200.0, dt_s=10.0, t_offset_s=0.0)
    assert hg.q_m3s[0] == 0.0


def test_triangular_zero_flow_before_offset_start():
    hg = triangular(Q_p=500.0, V=1_000_000.0, T_f=1200.0, dt_s=10.0, t_offset_s=300.0)
    before = hg.q_m3s[hg.t_s < 300.0]
    assert np.all(before == 0.0)
    assert hg.q_m3s[hg.t_s == 300.0][0] == 0.0
    assert hg.q_m3s[-1] == 0.0


def test_triangular_infeasible_base_time_raises():
    # T_b = 2V/Q_p must exceed T_f
    with pytest.raises(ValueError):
        triangular(Q_p=10_000.0, V=1_000_000.0, T_f=1200.0, dt_s=10.0)


@pytest.mark.parametrize("kwargs", [
    {"Q_p": 0.0, "V": 1e6, "T_f": 1200.0},
    {"Q_p": 500.0, "V": 0.0, "T_f": 1200.0},
    {"Q_p": 500.0, "V": 1e6, "T_f": 0.0},
])
def test_triangular_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        triangular(**kwargs)


# --- hydrograph_for_dam: method selection, blocking, M2 range check ----------


def test_hydrograph_for_dam_uses_weir_when_configured(make_config):
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert hg.method == "breach_growth_weir"
    assert hg.dam_id == cfg.dams[0].id


def test_hydrograph_for_dam_falls_back_to_triangular_without_hydrograph_config(make_config):
    cfg = make_config()  # no volume_elevation / breach_hydrograph attached
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0,
              "peak_discharge_m3s": 500.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert hg.method == "triangular"
    assert any(c["id"] == "triangular_fallback" for c in hg.caveats)


def test_hydrograph_for_dam_falls_back_with_placeholder_coefficient(make_config):
    bh = dict(SYNTH_BREACH_HYDROGRAPH)
    bh["side_slope_z"] = _placeholder("-")
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION, breach_hydrograph=bh)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0,
              "peak_discharge_m3s": 500.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert hg.method == "triangular"


def test_hydrograph_for_dam_blocked_without_any_method_available(make_config):
    cfg = make_config()  # no hydrograph config, no Q_p given
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    with pytest.raises(HydrographBlocked):
        hydrograph_for_dam(cfg.dams[0], params)


def test_hydrograph_for_dam_explicit_weir_request_blocked_raises(make_config):
    cfg = make_config()  # no hydrograph config
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0,
              "method": "breach_growth_weir"}
    with pytest.raises(HydrographBlocked):
        hydrograph_for_dam(cfg.dams[0], params)


def test_hydrograph_for_dam_explicit_triangular_request(make_config):
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0,
              "peak_discharge_m3s": 500.0, "method": "triangular"}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert hg.method == "triangular"


def test_hydrograph_for_dam_qp_range_checked_for_hd_dam(make_config):
    """dams[0] in synth.yaml is HD/moraine. Feature 3 (F3-D1) enabled XZ9 peak discharge, so the
    Qp pair is computable and the hydrograph's peak is checked against it (True/False, no caveat)."""
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert isinstance(hg.peak_within_m2_range, bool)
    assert not any(c["id"] == "m2_qp_range_blocked" for c in hg.caveats)


def test_hydrograph_for_dam_qp_range_blocked_for_fd_dam(make_config):
    """An FD dam's Qp pair stays blocked (Z20 has no FD branch, F3-D2): peak_within_m2_range is
    None with caveat m2_qp_range_blocked, not a tuned True/False."""
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    dam = cfg.dams[0].model_copy(update={"breach_inputs": cfg.dams[0].breach_inputs.model_copy(
        update={"dam_type": cfg.dams[0].breach_inputs.dam_type.model_copy(update={"value": "FD"})})})
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    hg = hydrograph_for_dam(dam, params)
    assert hg.peak_within_m2_range is None
    assert any(c["id"] == "m2_qp_range_blocked" for c in hg.caveats)


def test_qp_range_check_pure_helper_true_and_false():
    from backend.m2_breach.hydrograph import _qp_range_check

    class _FakeDam:
        kind = "embankment_dam"

    # We only need compute_dam(dam)'s output shape; patch it via monkeypatching would need a
    # real Dam. Instead test the comparison logic directly against a constructed range dict.
    def fake_compute_dam(dam):
        return {"parameters": {"peak_discharge_m3s": {"status": "ok", "low": 100.0, "high": 200.0}}}

    orig = hg_mod.compute_dam
    hg_mod.compute_dam = fake_compute_dam
    try:
        inside, caveats = _qp_range_check(150.0, _FakeDam())
        assert inside is True and caveats == []
        outside, caveats = _qp_range_check(500.0, _FakeDam())
        assert outside is False and caveats == []
    finally:
        hg_mod.compute_dam = orig


def test_hydrograph_for_dam_has_placeholders_reflects_missing_input(synth_raw, make_config):
    """dams[0] as shipped (without fully_sourced) has a placeholder input; make_config always
    fully sources breach_inputs, so build a placeholder case directly from the raw fixture."""
    import copy
    import warnings

    from backend.shared.site_config import SiteConfig

    raw = copy.deepcopy(synth_raw)
    dam = raw["dams"][0]
    dam["volume_elevation"] = SYNTH_VOLUME_ELEVATION_RELATION
    dam["breach_hydrograph"] = SYNTH_BREACH_HYDROGRAPH
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = SiteConfig.model_validate(raw)

    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0,
              "peak_discharge_m3s": 500.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    # water_volume_above_invert is a placeholder in synth.yaml as shipped -> weir blocked -> triangular
    assert hg.method == "triangular"
    assert hg.has_placeholders is True
    assert any(c["id"] == "placeholder_data" for c in hg.caveats)


def test_hydrograph_for_dam_missing_required_param_raises(make_config):
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    with pytest.raises(ValueError):
        hydrograph_for_dam(cfg.dams[0], {"water_volume_m3": 1_000_000.0})


def test_moraine_extrapolation_caveat_present(make_config):
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)
    assert any(c["id"] == "moraine_extrapolation" for c in hg.caveats)


# --- write_hydrograph: sidecar validates + CSV shape -------------------------


def test_write_hydrograph_validates_and_writes_files(make_config, tmp_path):
    cfg = make_config(volume_elevation=SYNTH_VOLUME_ELEVATION_RELATION,
                       breach_hydrograph=SYNTH_BREACH_HYDROGRAPH)
    params = {"water_volume_m3": 1_000_000.0, "breach_width_m": 40.0, "failure_time_s": 1200.0}
    hg = hydrograph_for_dam(cfg.dams[0], params)

    csv_path, json_path = write_hydrograph(hg, cfg.site.id, "synth__s001", data_dir=tmp_path)

    assert csv_path.is_file() and json_path.is_file()
    lines = csv_path.read_text().splitlines()
    assert lines[0] == "t_s,q_m3s"
    assert len(lines) > 2

    import json
    sidecar = json.loads(json_path.read_text())
    assert sidecar["scenario_id"] == "synth__s001"
    assert sidecar["dam_id"] == cfg.dams[0].id
    assert sidecar["method"] == "breach_growth_weir"

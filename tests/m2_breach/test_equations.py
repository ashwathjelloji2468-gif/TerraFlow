"""Unit tests for the base equations (`docs/Equations.md` §1-3, §5).

Expected values are computed independently by hand (see the docstring math),
not copied from the implementation — a genuine check, not a mirror.
"""

from __future__ import annotations

import pytest

from backend.m2_breach.f8 import breach_width_f8, failure_time_f8
from backend.m2_breach.f16 import peak_discharge_f16
from backend.m2_breach.f95 import breach_width_f95, failure_time_f95
from backend.m2_breach.h14 import peak_discharge_h14
from backend.m2_breach.mclm import failure_time_mclm
from backend.m2_breach.result import BlockedEquationError
from backend.m2_breach.xz9 import XZ9_REFERENCE_HEIGHT_M, breach_width_xz9, peak_discharge_xz9
from backend.m2_breach.z20 import peak_discharge_z20

G = 9.81


# ---------------------------------------------------------------- F16 ----

def test_f16_overtopping_hb_below_threshold():
    r = peak_discharge_f16(V_w=1e6, h_w=10, h_b=5, W_ave=50, failure_mode="O")
    assert r.value == pytest.approx(226.74059551886606)
    assert r.code == "F16"
    assert r.unit == "m3s"


def test_f16_piping_hb_above_threshold():
    r = peak_discharge_f16(V_w=1e6, h_w=10, h_b=10, W_ave=50, failure_mode="P")
    assert r.value == pytest.approx(260.7482010505135)


def test_f16_k_h_continuous_at_threshold():
    below = peak_discharge_f16(V_w=1e6, h_w=10, h_b=6.1, W_ave=50, failure_mode="O")
    just_above = peak_discharge_f16(V_w=1e6, h_w=10, h_b=6.1000001, W_ave=50, failure_mode="O")
    assert below.value == pytest.approx(just_above.value, rel=1e-5)


def test_f16_scaling_dimension_check():
    """Scaling every length by lambda should scale Q_p by lambda^2.5
    (V_w ~ length^3, h_w/h_b/W_ave ~ length^1; the sqrt(g*h_w*V_w*h_b^2/W_ave)
    term is length^((1+3+2-1)/2) = length^2.5)."""
    # h_b stays <= 6.1 m before and after scaling, so k_h == 1 in both cases
    # and the pure length-power relationship holds.
    base = peak_discharge_f16(V_w=1e6, h_w=10, h_b=2, W_ave=50, failure_mode="O")
    lam = 2.0
    scaled = peak_discharge_f16(V_w=1e6 * lam**3, h_w=10 * lam, h_b=2 * lam, W_ave=50 * lam, failure_mode="O")
    assert scaled.value == pytest.approx(base.value * lam**2.5)


def test_f16_rejects_nonpositive_inputs():
    with pytest.raises(ValueError):
        peak_discharge_f16(V_w=0, h_w=10, h_b=5, W_ave=50, failure_mode="O")


def test_f16_rejects_bad_failure_mode():
    with pytest.raises(ValueError):
        peak_discharge_f16(V_w=1e6, h_w=10, h_b=5, W_ave=50, failure_mode="X")


# ---------------------------------------------------------------- XZ9 ----

def test_xz9_reference_height_is_model_constant():
    assert XZ9_REFERENCE_HEIGHT_M == 15.0


def test_xz9_peak_discharge_is_enabled_and_unverified():
    """Feature 3 (F3-D1): computed from docs/Equations.md §1.2, still verified=False. Hand check:
    0.175*sqrt(9.81)*1e6^(5/6)*(15/15)^0.199*(100/10)^-1.274*exp(-0.649-0.705-0.375)."""
    import math

    r = peak_discharge_xz9(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="HD",
                           failure_mode="O", erodibility="M")
    expected = 0.175 * math.sqrt(9.81) * 1e5 * 1.0 * 10 ** -1.274 * math.exp(-1.729)
    assert r.value == pytest.approx(expected, rel=1e-12)
    assert r.value == pytest.approx(517.5, rel=1e-3)
    assert r.verified is False and r.unit == "m3s"
    assert r.source_tag.startswith("SECONDARY")


def test_xz9_breach_width_calculates_without_site_reference_height():
    r = breach_width_xz9(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="HD",
                         failure_mode="O", erodibility="M")
    assert r.value == pytest.approx(14.213558919371463)
    assert r.value > 0
    assert r.coefficients["h_r_m"] == 15.0
    assert r.verified is False


@pytest.mark.parametrize("dam_type,b3", [("CD", -0.041), ("FD", 0.026), ("HD", -0.226), ("ZD", -0.226)])
def test_xz9_width_dam_type_coefficients(dam_type, b3):
    r = breach_width_xz9(1e6, 10, 5, 15, dam_type, "O", "M")
    assert r.coefficients["b3_dam_type"] == b3


@pytest.mark.parametrize("failure_mode,b4", [("O", 0.149), ("P", -0.389)])
def test_xz9_width_failure_mode_coefficients(failure_mode, b4):
    r = breach_width_xz9(1e6, 10, 5, 15, "HD", failure_mode, "M")
    assert r.coefficients["b4_failure_mode"] == b4


@pytest.mark.parametrize("erodibility,b5", [("H", 0.291), ("M", -0.140), ("L", -0.391)])
def test_xz9_width_erodibility_coefficients(erodibility, b5):
    r = breach_width_xz9(1e6, 10, 5, 15, "HD", "O", erodibility)
    assert r.coefficients["b5_erodibility"] == b5


@pytest.mark.parametrize("kwargs", [
    {"V_w": 0}, {"h_w": 0}, {"h_b": 0}, {"h_d": 0},
    {"dam_type": "X"}, {"failure_mode": "X"}, {"erodibility": "X"},
])
def test_xz9_width_rejects_invalid_inputs(kwargs):
    values = dict(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="HD", failure_mode="O", erodibility="M")
    values.update(kwargs)
    with pytest.raises(ValueError):
        breach_width_xz9(**values)


# ---------------------------------------------------------------- Z20 ----

def test_z20_hd_branch():
    r = peak_discharge_z20(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="HD")
    assert r.value == pytest.approx(214.67473735501653)


def test_z20_cd_branch_differs_from_hd():
    hd = peak_discharge_z20(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="HD")
    cd = peak_discharge_z20(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type="CD")
    assert hd.value != pytest.approx(cd.value)


@pytest.mark.parametrize("dam_type", ["FD", "ZD"])
def test_z20_blocked_for_unmapped_dam_type(dam_type):
    with pytest.raises(BlockedEquationError):
        peak_discharge_z20(V_w=1e6, h_w=10, h_b=5, h_d=15, dam_type=dam_type)


# ---------------------------------------------------------------- F95 ----

def test_f95_breach_width_overtopping():
    r = breach_width_f95(V_w=1e6, h_b=5, failure_mode="O")
    assert r.value == pytest.approx(28.50546062387974)
    assert r.unit == "m"


def test_f95_breach_width_piping_smaller_than_overtopping():
    o = breach_width_f95(V_w=1e6, h_b=5, failure_mode="O")
    p = breach_width_f95(V_w=1e6, h_b=5, failure_mode="P")
    assert p.value < o.value  # K_n: O=1.4 > P=1.0


def test_f95_failure_time_converted_to_seconds():
    r = failure_time_f95(V_w=1e6, h_b=5)
    assert r.unit == "s"
    assert r.value == pytest.approx(3251.3461872482085)
    # sanity: matches the hours formula * 3600
    hours = 0.00254 * 1e6**0.53 * 5 ** (-0.9)
    assert r.value == pytest.approx(hours * 3600)


# ----------------------------------------------------------------- F8 ----

def test_f8_breach_width_overtopping():
    r = breach_width_f8(V_w=1e6, h_b=5, failure_mode="O")
    assert r.value == pytest.approx(31.136222110259812)


def test_f8_failure_time_seconds_matches_hours_times_3600():
    r = failure_time_f8(V_w=1e6, h_b=5)
    assert r.unit == "s"
    assert r.value == pytest.approx(4035.641415065118)
    hours = r.value / 3600.0
    # docs/Equations.md §3.2: T_f[h] = 63.2*sqrt(V_w/(g*h_b^2))/3600
    expected_hours = 63.2 * (1e6 / (G * 5**2)) ** 0.5 / 3600
    assert hours == pytest.approx(expected_hours)


# --------------------------------------------------------------- MCLM ----

def test_mclm_failure_time():
    r = failure_time_mclm(V_w=1e6, h_w=10)
    assert r.unit == "s"
    assert r.value == pytest.approx(1556.7707977833895)


# ---------------------------------------------------------------- H14 ----

def test_h14_peak_discharge():
    r = peak_discharge_h14(V_w=1e6, h_w=10)
    assert r.value == pytest.approx(285.12536938988274)


# ----------------------------------------------------------- positivity ----

@pytest.mark.parametrize("fn,kwargs", [
    (breach_width_f95, {"V_w": -1, "h_b": 5, "failure_mode": "O"}),
    (breach_width_f8, {"V_w": 1e6, "h_b": 0, "failure_mode": "O"}),
    (failure_time_mclm, {"V_w": 1e6, "h_w": -5}),
    (peak_discharge_h14, {"V_w": 0, "h_w": 10}),
])
def test_nonpositive_inputs_raise(fn, kwargs):
    with pytest.raises(ValueError):
        fn(**kwargs)

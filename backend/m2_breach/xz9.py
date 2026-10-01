"""Xu & Zhang (2009) peak discharge and breach width — code XZ9.

Both equations are SECONDARY: transcribed in `docs/Equations.md` §1.2 (Q_p, Azmi p.6, Table 1,
footnote b) and §2.3 (B_ave, Azmi p.8, Table 3, footnote c) from Azmi's reproduction, not from the
original paper (Xu & Zhang 2009, *Breaching parameters for earth and rockfill dams*, J Geotech
Geoenviron Eng 135(12):1957-1970, doi:10.1061/(asce)gt.1943-5606.0000162), which is not in hand.
Every result therefore carries `verified=False`. Only the reference height h_r = 15.0 m is
PRIMARY (Xu & Zhang 2009, `docs/Equations.md` §0). g is NOT STATED by Azmi; SI units imply
9.81 m/s^2, the same value F16 and F8 use (docs/decisions.md 2026-10-01 "Feature 3: XZ9 peak
discharge enabled").

Inputs use SI units: V_w [m3], h_w [m], h_b [m], h_d [m], dam_type
(HD|CD|FD|ZD), failure_mode (O|P), and erodibility (H|M|L).
Valid range: NOT AVAILABLE (docs/Equations.md) -- only positivity is checked.
"""

from __future__ import annotations

from math import exp

from .result import MethodResult, check_positive

XZ9_REFERENCE_HEIGHT_M = 15.0
_G = 9.81  # m/s^2 — NOT STATED by Azmi; SI units imply this value (docs/Equations.md §0)

# docs/Equations.md §1.2 (Q_p) coefficient table, by dam type / failure mode / erodibility.
_QP_B3_DAM_TYPE = {"CD": -0.503, "FD": -0.591, "HD": -0.649, "ZD": -0.649}
_QP_B4_FAILURE_MODE = {"O": -0.705, "P": -1.039}
_QP_B5_ERODIBILITY = {"H": -0.007, "M": -0.375, "L": -1.362}
_QP_SOURCE_TAG = "SECONDARY (Azmi p.6, Table 1, footnote b)"
_BAVE_SOURCE_TAG = "SECONDARY (Azmi p.8, Table 3, footnote c); h_r PRIMARY (Xu & Zhang 2009)"

_B3_DAM_TYPE = {"CD": -0.041, "FD": 0.026, "HD": -0.226, "ZD": -0.226}
_B4_FAILURE_MODE = {"O": 0.149, "P": -0.389}
_B5_ERODIBILITY = {"H": 0.291, "M": -0.140, "L": -0.391}


def _lookup(table: dict, key, name: str) -> float:
    try:
        return table[key]
    except (KeyError, TypeError):
        raise ValueError(f"{name} must be one of {tuple(table)}, got {key!r}") from None


def peak_discharge_xz9(V_w: float, h_w: float, h_b: float, h_d: float, dam_type: str,
                       failure_mode: str, erodibility: str) -> MethodResult:
    """Xu & Zhang (2009) peak discharge, in m^3/s (`docs/Equations.md` §1.2).

    Q_p = 0.175 * sqrt(g) * V_w^(5/6) * (h_d/h_r)^0.199 * (V_w^(1/3)/h_w)^(-1.274) * exp(B4),
    B4 = b3 + b4 + b5. Units: sqrt(g) * V_w^(5/6) = m^0.5/s * m^2.5 = m^3/s; the other factors are
    dimensionless. `h_b` is accepted for a uniform XZ9 signature but does not appear in Q_p.
    """
    check_positive(V_w=V_w, h_w=h_w, h_b=h_b, h_d=h_d)
    b3 = _lookup(_QP_B3_DAM_TYPE, dam_type, "dam_type")
    b4 = _lookup(_QP_B4_FAILURE_MODE, failure_mode, "failure_mode")
    b5 = _lookup(_QP_B5_ERODIBILITY, erodibility, "erodibility")
    b_sum = b3 + b4 + b5
    value = (0.175 * _G ** 0.5 * V_w ** (5.0 / 6.0) * (h_d / XZ9_REFERENCE_HEIGHT_M) ** 0.199
             * (V_w ** (1.0 / 3.0) / h_w) ** (-1.274) * exp(b_sum))
    return MethodResult(
        code="XZ9", value=value, unit="m3s",
        branch=f"B4={b_sum:.3f} (b3={b3} {dam_type}, b4={b4} {failure_mode}, b5={b5} {erodibility})",
        coefficients={"prefactor": 0.175, "g": _G, "volume_exponent": 5.0 / 6.0,
                      "h_d_over_h_r_exponent": 0.199, "volume_depth_ratio_exponent": -1.274,
                      "b3_dam_type": b3, "b4_failure_mode": b4, "b5_erodibility": b5,
                      "h_r_m": XZ9_REFERENCE_HEIGHT_M},
        source_tag=_QP_SOURCE_TAG, verified=False,
    )


def breach_width_xz9(V_w: float, h_w: float, h_b: float, h_d: float, dam_type: str,
                     failure_mode: str, erodibility: str) -> MethodResult:
    """Xu & Zhang (2009) average breach width, in metres.

    B_ave = 0.787*h_b*(h_d/h_r)^0.133*(V_w^(1/3)/h_w)^0.652*exp(B3),
    B3 = b3 + b4 + b5.
    """
    check_positive(V_w=V_w, h_w=h_w, h_b=h_b, h_d=h_d)
    try:
        b3 = _B3_DAM_TYPE[dam_type]
    except (KeyError, TypeError):
        raise ValueError(f"dam_type must be one of {tuple(_B3_DAM_TYPE)}, got {dam_type!r}") from None
    try:
        b4 = _B4_FAILURE_MODE[failure_mode]
    except (KeyError, TypeError):
        raise ValueError(f"failure_mode must be one of {tuple(_B4_FAILURE_MODE)}, got {failure_mode!r}") from None
    try:
        b5 = _B5_ERODIBILITY[erodibility]
    except (KeyError, TypeError):
        raise ValueError(f"erodibility must be one of {tuple(_B5_ERODIBILITY)}, got {erodibility!r}") from None

    b3_sum = b3 + b4 + b5
    value = (0.787 * h_b * (h_d / XZ9_REFERENCE_HEIGHT_M) ** 0.133
             * (V_w ** (1.0 / 3.0) / h_w) ** 0.652 * exp(b3_sum))
    return MethodResult(
        code="XZ9", value=value, unit="m",
        branch=f"B3={b3_sum} (b3={b3}, b4={b4}, b5={b5})",
        coefficients={"prefactor": 0.787, "h_d_over_h_r_exponent": 0.133,
                      "volume_depth_ratio_exponent": 0.652,
                      "b3_dam_type": b3, "b4_failure_mode": b4, "b5_erodibility": b5,
                      "h_r_m": XZ9_REFERENCE_HEIGHT_M},
        source_tag=_BAVE_SOURCE_TAG, verified=False,
    )


def xz9_result(output: str) -> MethodResult:
    """A blocked XZ9 result for callers that have no physical inputs.

    Calculations must use :func:`peak_discharge_xz9` / :func:`breach_width_xz9` with the dam inputs.
    """
    if output == "breach_width_m":
        return MethodResult.blocked("XZ9", "m", "XZ9 breach width requires physical inputs; call breach_width_xz9")
    return MethodResult.blocked("XZ9", "m3s", "XZ9 peak discharge requires physical inputs; call peak_discharge_xz9")

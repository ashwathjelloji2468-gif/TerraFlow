"""Reproduces (in ballpark) `docs/paper_azmi.md` Table 7's median % error per
method, over `tests/data/breach_cases.csv`.

PE% = 100*(model-observed)/observed, exactly as Azmi defines it
(`docs/paper_azmi.md` "What the paper does"). XZ9, DFM_2024 and the Qp/Bave
DFM_updated outputs are not reproduced here because this case table lacks the
physical inputs needed by XZ9.

Pass rule, fixed here before running, not adjusted afterwards: the computed
median PE must fall within the paper's own reported MAD of the paper's
median PE (Table 7 columns "MPE median" / "MPE MAD"). This is a deliberately
generous, principled band — not a fit to whatever this implementation
happens to produce.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

import pytest

from backend.m2_breach.f8 import breach_width_f8, failure_time_f8
from backend.m2_breach.f16 import peak_discharge_f16
from backend.m2_breach.f95 import breach_width_f95, failure_time_f95
from backend.m2_breach.mclm import failure_time_mclm
from backend.m2_breach.z20 import peak_discharge_z20

CSV_PATH = Path(__file__).resolve().parents[2] / "tests" / "data" / "breach_cases.csv"

# Missing-artifact gate: the case table is not in the repository (the root .gitignore `data/`
# pattern also matches tests/data/, and scripts/build_breach_cases.py named in
# docs/data_sources.md is absent). Skip clearly instead of erroring in a fresh checkout.
requires_case_table = pytest.mark.skipif(
    not CSV_PATH.is_file(),
    reason="missing test artifact tests/data/breach_cases.csv "
           "(historical dam-break case table, not committed; see docs/data_sources.md)",
)

# docs/paper_azmi.md Table 7: {method: (output, paper_median_pe, paper_mad)}
TABLE_7 = {
    "F16_Qp": ("peak_outflow_m3s", -10.1, 31.9),
    "Z20_Qp": ("peak_outflow_m3s", -26.2, 34.0),
    "F95_Bave": ("breach_avg_width_m", -5.2, 23.2),
    "F8_Bave": ("breach_avg_width_m", -1.8, 29.8),
    "F95_Tf": ("failure_time_s", -11.3, 36.6),
    "F8_Tf": ("failure_time_s", -7.3, 35.3),
    "MCLM_Tf": ("failure_time_s", 3.4, 59.4),
}


def _rows() -> list[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(row: dict, col: str) -> float | None:
    v = row[col].strip()
    return float(v) if v else None


def _h_d(row: dict) -> float | None:
    """Azmi's gap-filling: missing h_d -> h_d = h_b (docs/paper_azmi.md)."""
    hd = _f(row, "dam_height_m")
    return hd if hd is not None else _f(row, "breach_depth_m")


def _pe(model: float, observed: float) -> float:
    return 100.0 * (model - observed) / observed


def _has_observed(row: dict, code: str) -> bool:
    return code in row["observed_params"].split(";")


def _medians() -> dict[str, list[float]]:
    """method -> list of PE% values, one per usable case."""
    pes: dict[str, list[float]] = {k: [] for k in TABLE_7}

    for row in _rows():
        V_w, h_w, h_b = _f(row, "water_volume_m3"), _f(row, "height_of_water_m"), _f(row, "breach_depth_m")
        h_d = _h_d(row)
        W_ave = _f(row, "average_width_m")
        dam_type = row["dam_type_code"].strip() or None
        failure_mode = row["failure_mode"].strip() or None

        obs_qp = _f(row, "peak_outflow_m3s")
        obs_bave = _f(row, "breach_avg_width_m")
        obs_tf = _f(row, "failure_time_s")

        if V_w and h_w and h_b:
            if _has_observed(row, "Qp") and obs_qp:
                if W_ave and failure_mode:
                    try:
                        r = peak_discharge_f16(V_w, h_w, h_b, W_ave, failure_mode)
                        pes["F16_Qp"].append(_pe(r.value, obs_qp))
                    except ValueError:
                        pass
                if h_d and dam_type in ("HD", "CD"):
                    r = peak_discharge_z20(V_w, h_w, h_b, h_d, dam_type)
                    pes["Z20_Qp"].append(_pe(r.value, obs_qp))

            if _has_observed(row, "Bave") and obs_bave and failure_mode:
                r = breach_width_f95(V_w, h_b, failure_mode)
                pes["F95_Bave"].append(_pe(r.value, obs_bave))
                r = breach_width_f8(V_w, h_b, failure_mode)
                pes["F8_Bave"].append(_pe(r.value, obs_bave))

            if _has_observed(row, "Tf") and obs_tf:
                r = failure_time_f95(V_w, h_b)
                pes["F95_Tf"].append(_pe(r.value, obs_tf))
                r = failure_time_f8(V_w, h_b)
                pes["F8_Tf"].append(_pe(r.value, obs_tf))
                r = failure_time_mclm(V_w, h_w)
                pes["MCLM_Tf"].append(_pe(r.value, obs_tf))

    return pes


@pytest.fixture(scope="module")
def median_pe_by_method() -> dict[str, list[float]]:
    return _medians()


@requires_case_table
@pytest.mark.parametrize("method", sorted(TABLE_7))
def test_median_pe_in_paper_ballpark(method, median_pe_by_method):
    output, paper_median, paper_mad = TABLE_7[method]
    values = median_pe_by_method[method]
    assert len(values) >= 5, f"{method}: too few usable cases ({len(values)}) to compare"

    our_median = statistics.median(values)
    print(f"\n{method}: n={len(values)} our_median={our_median:.1f}% "
          f"paper_median={paper_median:.1f}% paper_MAD={paper_mad:.1f}")

    assert abs(our_median - paper_median) <= paper_mad, (
        f"{method}: our median PE {our_median:.1f}% is more than the paper's own MAD "
        f"({paper_mad:.1f}) away from its reported median ({paper_median:.1f}%)"
    )

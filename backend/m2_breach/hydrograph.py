"""Breach hydrographs — contract §4.2 (`docs/handoff_contract.md`).

`hydrograph(site_id, dam_id, params)` is the in-process function used by M0, M5 Monte Carlo and
the M3/M4 case generators. `params` carries a scenario's breach parameters (as sampled by M5's
Monte Carlo or a scenario design, not recomputed here):

    {
      "water_volume_m3": 0.0,      # required; initial reservoir volume above the invert
      "breach_width_m": 0.0,       # required; final average breach width (B_ave)
      "failure_time_s": 0.0,       # required; breach formation time (T_f)
      "peak_discharge_m3s": 0.0,   # required only for the triangular method/fallback
      "method": "breach_growth_weir",   # optional; default: weir if configured, else triangular
      "dt_s": 10.0, "t_offset_s": 0.0,  # optional
    }

Two methods:

- **`breach_growth_weir`** (`breach_growth_weir()` below): the breach grows linearly (both in
  width and invert depth) over `failure_time_s`, discharge from `weir.weir_discharge`, level-pool
  routed against a `storage.StorageCurve`. Needs `Dam.volume_elevation` and
  `Dam.breach_hydrograph` fully sourced (`backend/shared/site_config.py`); otherwise blocked.
- **`triangular`** (`triangular()` below): a volume-conserving triangular hydrograph from
  `peak_discharge_m3s` and `water_volume_m3`, used as a fallback (contract's
  `method: triangular`) when the weir method's prerequisites are missing.

`hydrograph_for_dam(dam, params)` does the actual work for an already-loaded `Dam`
(`backend/m2_breach/breach_params.py`'s `compute_dam` follows the same split). `hydrograph()` is
the thin, contract-shaped wrapper that loads the site config and looks up the dam.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from scipy.integrate import solve_ivp

from backend.shared.site_config import Dam, SiteConfig, _placeholder_paths, default_data_dir, load_site_config

from . import storage as storage_mod
from .breach_params import compute_dam
from .weir import weir_discharge

CONTRACT_VERSION = "0.3.0"
# Output root: `$SIH26_DATA_DIR`, else `<repo>/data` (`site_config.default_data_dir`), resolved per call.

_DEFAULT_DT_S = 10.0
_DEFAULT_T_OFFSET_S = 0.0
_DRAIN_FRACTION = 0.999  # stop the ODE once this fraction of V0 has been released
_T_MAX_MULTIPLIER = 50.0  # ODE integration horizon, as a multiple of T_f (a numerical-integration
                          # setting, not a physical coefficient — no source needed)


class HydrographBlocked(Exception):
    """Raised when no hydrograph method can be evaluated for the given dam/params."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _caveat(id_: str, severity: str = "warning") -> dict:
    return {"id": id_, "severity": severity, "text_key": f"caveat_{id_}"}


@dataclass(frozen=True)
class Hydrograph:
    t_s: np.ndarray
    q_m3s: np.ndarray
    method: str
    params: dict
    dt_s: float
    t_offset_s: float
    peak_q_m3s: float
    time_to_peak_s: float
    volume_m3: float
    mass_balance_error_pct: float
    caveats: list[dict] = field(default_factory=list)
    dam_id: str = ""
    scenario_id: str | None = None
    peak_within_m2_range: bool | None = None
    has_placeholders: bool = False
    provenance: dict = field(default_factory=dict)
    trigger: dict | None = None

    def to_csv(self) -> str:
        lines = ["t_s,q_m3s"]
        lines += [f"{t:.6g},{q:.6g}" for t, q in zip(self.t_s, self.q_m3s)]
        return "\n".join(lines) + "\n"

    def sidecar(self) -> dict:
        d = {
            "contract_version": CONTRACT_VERSION,
            "scenario_id": self.scenario_id,
            "dam_id": self.dam_id,
            "method": self.method,
            "params": self.params,
            "dt_s": self.dt_s,
            "t_offset_s": self.t_offset_s,
            "peak_q_m3s": self.peak_q_m3s,
            "time_to_peak_s": self.time_to_peak_s,
            "volume_m3": self.volume_m3,
            "mass_balance_error_pct": self.mass_balance_error_pct,
            "peak_within_m2_range": self.peak_within_m2_range,
            "has_placeholders": self.has_placeholders,
            "caveats": self.caveats,
            "provenance": self.provenance,
        }
        if self.trigger is not None:
            d["trigger"] = self.trigger
        return d


def _qp_range_check(peak_q_m3s: float, dam: Dam) -> tuple[bool | None, list[dict]]:
    """Compare `peak_q_m3s` against the M2 dual-method Q_p range (`breach_params.compute_dam`).

    Returns `(None, [caveat])` when that range is blocked (for example, because XZ9 inputs are
    placeholders), rather than guessing — a `None` flag, not `False`, so callers don't read
    "blocked" as "outside range".
    """
    qp = compute_dam(dam)["parameters"]["peak_discharge_m3s"]
    if qp.get("status") == "blocked":
        return None, [_caveat("m2_qp_range_blocked", severity="info")]
    return bool(qp["low"] <= peak_q_m3s <= qp["high"]), []


def breach_growth_weir(storage_curve: storage_mod.StorageCurve, h0: float, B_ave: float, h_b: float,
                       T_f: float, C_r: float, C_s: float, z: float,
                       dt_s: float = _DEFAULT_DT_S, t_offset_s: float = _DEFAULT_T_OFFSET_S) -> Hydrograph:
    """Level-pool-routed breach hydrograph.

    The breach grows linearly over `[t_offset_s, t_offset_s + T_f]`: bottom width from 0 to
    `b_f = B_ave - z*h_b` (B_ave is the *mean* of top and bottom width — `docs/Equations.md` §0),
    and invert elevation (above the final invert) from `h0` down to 0. Both reach zero head at
    breach start (`Q=0`) regardless of side slope.

    `h0` is the initial head above the final invert, i.e. `storage_curve.head(water_volume_m3)`.
    """
    if T_f <= 0:
        raise ValueError(f"failure_time_s (T_f) must be > 0, got {T_f!r}")
    if h0 <= 0:
        raise ValueError(f"initial head h0 must be > 0, got {h0!r}")
    b_f = B_ave - z * h_b
    if b_f <= 0:
        raise ValueError(f"final bottom width B_ave - z*h_b = {b_f!r} must be > 0 "
                          f"(B_ave={B_ave!r}, z={z!r}, h_b={h_b!r})")

    V0 = storage_curve.volume(h0)

    def tau(t: float) -> float:
        return min(max((t - t_offset_s) / T_f, 0.0), 1.0)

    def b(t: float) -> float:
        return b_f * tau(t)

    def y(t: float) -> float:
        return h0 * (1.0 - tau(t))

    def rhs(t: float, v: np.ndarray) -> list[float]:
        head = storage_curve.head(v[0]) - y(t)
        q = weir_discharge(b(t), z, head, C_r, C_s)
        return [-q]

    def drained(t: float, v: np.ndarray) -> float:
        return v[0] - (1.0 - _DRAIN_FRACTION) * V0

    drained.terminal = True
    drained.direction = -1

    t_max = t_offset_s + _T_MAX_MULTIPLIER * T_f
    sol = solve_ivp(rhs, (t_offset_s, t_max), [V0], method="RK45", max_step=dt_s,
                     events=drained, dense_output=True)
    t_end = float(sol.t[-1])

    n_steps = max(int(round((t_end - 0.0) / dt_s)), 1)
    t_grid = np.linspace(0.0, t_end, n_steps + 1)
    if t_offset_s > 0:
        t_grid = np.union1d(t_grid, [t_offset_s])

    v_grid = np.where(t_grid < t_offset_s, V0, sol.sol(np.clip(t_grid, t_offset_s, t_end))[0])
    v_grid = np.clip(v_grid, 0.0, V0)
    head_grid = np.array([storage_curve.head(v) for v in v_grid]) - np.array([y(t) for t in t_grid])
    q_grid = np.array([weir_discharge(b(t), z, h, C_r, C_s) for t, h in zip(t_grid, head_grid)])
    q_grid = np.where(t_grid < t_offset_s, 0.0, q_grid)

    v_end = float(v_grid[-1])
    released_ode = V0 - v_end
    volume = float(np.trapezoid(q_grid, t_grid))
    mass_balance_error_pct = (volume - released_ode) / released_ode * 100.0 if released_ode > 0 else 0.0

    caveats = [_caveat(c, severity="info") for c in storage_curve.caveats]
    if v_end > (1.0 - _DRAIN_FRACTION) * V0:
        caveats.append(_caveat("hydrograph_did_not_fully_drain"))

    peak_idx = int(np.argmax(q_grid))
    return Hydrograph(
        t_s=t_grid, q_m3s=q_grid, method="breach_growth_weir",
        params={"breach_width_m": B_ave, "failure_time_s": T_f, "water_volume_m3": V0},
        dt_s=dt_s, t_offset_s=t_offset_s,
        peak_q_m3s=float(q_grid[peak_idx]), time_to_peak_s=float(t_grid[peak_idx] - t_offset_s),
        volume_m3=volume, mass_balance_error_pct=mass_balance_error_pct, caveats=caveats,
    )


def triangular(Q_p: float, V: float, T_f: float,
               dt_s: float = _DEFAULT_DT_S, t_offset_s: float = _DEFAULT_T_OFFSET_S) -> Hydrograph:
    """Volume-conserving triangular hydrograph: rises to `Q_p` at `t_offset_s + T_f`, falls to
    zero at `t_offset_s + T_b`, `T_b = 2*V/Q_p` chosen so the triangle's area equals `V` exactly.
    """
    if Q_p <= 0:
        raise ValueError(f"peak_discharge_m3s (Q_p) must be > 0, got {Q_p!r}")
    if V <= 0:
        raise ValueError(f"water_volume_m3 (V) must be > 0, got {V!r}")
    if T_f <= 0:
        raise ValueError(f"failure_time_s (T_f) must be > 0, got {T_f!r}")

    T_b = 2.0 * V / Q_p
    if T_b <= T_f:
        raise ValueError(f"triangular hydrograph is infeasible: base time 2V/Q_p ({T_b:.1f} s) "
                          f"must exceed failure_time_s ({T_f!r} s) to conserve volume")

    t_rise, t_peak, t_end = t_offset_s, t_offset_s + T_f, t_offset_s + T_b
    grid = np.arange(0.0, t_end + dt_s, dt_s)
    t_all = np.union1d(grid[grid <= t_end], [t_rise, t_peak, t_end])

    q_all = np.interp(t_all, [t_rise, t_peak, t_end], [0.0, Q_p, 0.0])
    q_all = np.where(t_all < t_rise, 0.0, q_all)

    volume = float(np.trapezoid(q_all, t_all))
    mass_balance_error_pct = (volume - V) / V * 100.0

    peak_idx = int(np.argmax(q_all))
    return Hydrograph(
        t_s=t_all, q_m3s=q_all, method="triangular",
        params={"breach_width_m": None, "failure_time_s": T_f, "water_volume_m3": V},
        dt_s=dt_s, t_offset_s=t_offset_s,
        peak_q_m3s=float(q_all[peak_idx]), time_to_peak_s=float(t_all[peak_idx] - t_offset_s),
        volume_m3=volume, mass_balance_error_pct=mass_balance_error_pct,
        caveats=[_caveat("triangular_fallback", severity="info")],
    )


def _sourced(value_obj) -> bool:
    return value_obj is not None and value_obj.value is not None


def _weir_prereqs(dam: Dam) -> tuple[storage_mod.StorageCurve | None, float | None, str | None]:
    """Check `dam.volume_elevation` / `dam.breach_hydrograph` are fully sourced.

    Returns `(storage_curve, h_b, None)` if the weir method is available, or
    `(None, None, reason)` if not.
    """
    ve = dam.volume_elevation
    bh = dam.breach_hydrograph
    bi = dam.breach_inputs

    if ve is None:
        return None, None, "no volume_elevation configured for this dam"
    if bh is None:
        return None, None, "no breach_hydrograph (weir coefficients / side slope) configured for this dam"
    if not _sourced(bi.breach_height):
        return None, None, "breach_inputs.breach_height is a placeholder"
    if not (_sourced(bh.weir_coefficient_rect) and _sourced(bh.weir_coefficient_side)
            and _sourced(bh.side_slope_z)):
        return None, None, "breach_hydrograph has a placeholder coefficient"

    if ve.method == "surveyed_curve":
        if not (_sourced(ve.points) and _sourced(ve.breach_invert_elevation_m)):
            return None, None, "volume_elevation.points or breach_invert_elevation_m is a placeholder"
        curve = storage_mod.from_surveyed_curve(ve.points.value, ve.breach_invert_elevation_m.value)
    else:
        if not (_sourced(bi.water_volume_above_invert) and _sourced(bi.water_height_above_invert)
                and _sourced(ve.area_volume_exponent_b)):
            return None, None, ("volume_elevation.area_volume_exponent_b or "
                                 "breach_inputs.water_volume_above_invert/water_height_above_invert "
                                 "is a placeholder")
        curve = storage_mod.from_area_volume_relation(bi.water_volume_above_invert.value,
                                                       bi.water_height_above_invert.value,
                                                       ve.area_volume_exponent_b.value)
    return curve, bi.breach_height.value, None


def _code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, cwd=Path(__file__).resolve().parents[2], timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def hydrograph_for_dam(dam: Dam, params: dict) -> Hydrograph:
    """Build a `Hydrograph` for one `Dam` from scenario `params` (see module docstring)."""
    for key in ("water_volume_m3", "breach_width_m", "failure_time_s"):
        if key not in params:
            raise ValueError(f"params is missing required key {key!r}")

    V = params["water_volume_m3"]
    B_ave = params["breach_width_m"]
    T_f = params["failure_time_s"]
    Q_p = params.get("peak_discharge_m3s")
    dt_s = params.get("dt_s", _DEFAULT_DT_S)
    t_offset_s = params.get("t_offset_s", _DEFAULT_T_OFFSET_S)
    requested_method = params.get("method")

    curve, h_b, weir_block_reason = _weir_prereqs(dam)

    if requested_method == "triangular":
        use_weir = False
    elif requested_method == "breach_growth_weir":
        if curve is None:
            raise HydrographBlocked(f"breach_growth_weir requested but blocked: {weir_block_reason}")
        use_weir = True
    else:
        use_weir = curve is not None

    if use_weir:
        h0 = curve.head(V)
        bh = dam.breach_hydrograph
        hg = breach_growth_weir(curve, h0, B_ave, h_b, T_f,
                                 bh.weir_coefficient_rect.value, bh.weir_coefficient_side.value,
                                 bh.side_slope_z.value, dt_s=dt_s, t_offset_s=t_offset_s)
    else:
        if Q_p is None:
            reasons = [r for r in (weir_block_reason, "peak_discharge_m3s not given for the "
                                    "triangular method") if r]
            raise HydrographBlocked("; ".join(reasons))
        hg = triangular(Q_p, V, T_f, dt_s=dt_s, t_offset_s=t_offset_s)

    peak_within_range, range_caveats = _qp_range_check(hg.peak_q_m3s, dam)

    caveats = list(hg.caveats) + range_caveats + [_caveat("clear_water", severity="info")]
    if dam.kind in ("moraine_dammed_lake", "landslide_dam"):
        caveats.append(_caveat("moraine_extrapolation", severity="info"))

    placeholder_paths = [p for p in _dam_placeholder_paths(dam)]
    has_placeholders = bool(placeholder_paths)
    if has_placeholders:
        caveats.append(_caveat("placeholder_data"))

    provenance = {
        "method": "empirical", "code_version": _code_version(),
        "data_sources": ["docs/Equations.md", "docs/decisions.md"],
    }

    return replace(hg, dam_id=dam.id, peak_within_m2_range=peak_within_range,
                   has_placeholders=has_placeholders, caveats=caveats, provenance=provenance)


def _dam_placeholder_paths(dam: Dam) -> list[str]:
    return list(_placeholder_paths(dam, dam.id))


def hydrograph(site_id: str, dam_id: str, params: dict, sites_dir: str | Path | None = None) -> Hydrograph:
    """Contract §4.2's `hydrograph(site_id, dam_id, params) -> Hydrograph`."""
    cfg: SiteConfig = load_site_config(site_id, sites_dir=sites_dir)
    dam = next((d for d in cfg.dams if d.id == dam_id), None)
    if dam is None:
        raise ValueError(f"site '{site_id}' has no dam '{dam_id}'")
    return hydrograph_for_dam(dam, params)


def write_hydrograph(hg: Hydrograph, site_id: str, scenario_id: str, data_dir: Path | None = None) -> tuple[Path, Path]:
    """Validate and write `hydrographs/<scenario_id>__<dam_id>.csv` + `.json` sidecar.

    Returns `(csv_path, json_path)`.
    """
    from backend.m0_api import schemas  # local import: keep m2_breach importable without m0_api

    hg = replace(hg, scenario_id=scenario_id)
    sidecar = hg.sidecar()
    schemas.validate("hydrograph_sidecar.schema.json", sidecar)

    out_dir = Path(data_dir or default_data_dir()) / site_id / "breach" / "hydrographs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{scenario_id}__{hg.dam_id}"
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"
    csv_path.write_text(hg.to_csv())
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n")
    return csv_path, json_path

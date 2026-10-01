"""Per-dam breach parameters -> `breach_params.json` (contract §4.2, plus
the additive `status`/`reason` fields agreed for blocked methods — see
`docs/decisions.md`).

Orchestrates the base equations (f16, xz9, z20, f95, f8, mclm, h14), the DFM
fusion (dfm.py) and the dual-method ranges (ranges.py) for every dam in a
`SiteConfig` (`backend/shared/site_config.py`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.shared.site_config import Dam, SiteConfig, _placeholder_paths, default_data_dir

from . import dfm, f8, f16, f95, h14, mclm, ranges, xz9, z20
from .result import BlockedEquationError, MethodResult, warn_if_breach_exceeds_dam

CONTRACT_VERSION = "0.3.0"
# Output root: `$SIH26_DATA_DIR`, else `<repo>/data` (`site_config.default_data_dir`), resolved per call.


def _code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, cwd=Path(__file__).resolve().parents[2], timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _blocked_missing(code: str, unit: str, missing: list[str]) -> MethodResult:
    return MethodResult.blocked(code, unit, f"missing input(s): {', '.join(missing)} (status: placeholder)")


def _values(dam: Dam) -> tuple[dict[str, float | str | None], list[str]]:
    """Extract raw values from `dam.breach_inputs`, and the list of input
    names whose SourcedValue is null (placeholder)."""
    bi = dam.breach_inputs
    fields = {
        "V_w": bi.water_volume_above_invert,
        "h_w": bi.water_height_above_invert,
        "h_b": bi.breach_height,
        "h_d": bi.dam_height,
        "W_ave": bi.average_embankment_width,
        "dam_type": bi.dam_type,
        "failure_mode": bi.failure_mode,
        "erodibility": bi.erodibility,
    }
    values = {k: v.value for k, v in fields.items()}
    missing = [k for k, v in fields.items() if v.value is None]
    return values, missing


def _compute_peak_discharge(v: dict, missing: list[str]) -> dict[str, MethodResult]:
    out: dict[str, MethodResult] = {}

    if not ({"V_w", "h_w", "h_b", "W_ave", "failure_mode"} & set(missing)):
        out["F16"] = f16.peak_discharge_f16(v["V_w"], v["h_w"], v["h_b"], v["W_ave"], v["failure_mode"])
    else:
        out["F16"] = _blocked_missing("F16", "m3s", [m for m in ("V_w", "h_w", "h_b", "W_ave", "failure_mode") if m in missing])

    if not ({"V_w", "h_w", "h_b", "h_d", "dam_type"} & set(missing)):
        try:
            out["Z20"] = z20.peak_discharge_z20(v["V_w"], v["h_w"], v["h_b"], v["h_d"], v["dam_type"])
        except BlockedEquationError as e:
            out["Z20"] = MethodResult.blocked("Z20", "m3s", e.reason)
    else:
        out["Z20"] = _blocked_missing("Z20", "m3s", [m for m in ("V_w", "h_w", "h_b", "h_d", "dam_type") if m in missing])

    xz9_needed = ("V_w", "h_w", "h_b", "h_d", "dam_type", "failure_mode", "erodibility")
    if not (set(xz9_needed) & set(missing)):
        out["XZ9"] = xz9.peak_discharge_xz9(v["V_w"], v["h_w"], v["h_b"], v["h_d"], v["dam_type"],
                                             v["failure_mode"], v["erodibility"])
    else:
        out["XZ9"] = _blocked_missing("XZ9", "m3s", [m for m in xz9_needed if m in missing])

    if not ({"V_w", "h_w"} & set(missing)):
        out["H14"] = h14.peak_discharge_h14(v["V_w"], v["h_w"])
    else:
        out["H14"] = _blocked_missing("H14", "m3s", [m for m in ("V_w", "h_w") if m in missing])

    out["DFM_updated"] = dfm.peak_discharge_dfm_updated(out["F16"], out["XZ9"], out["Z20"])
    out["DFM_2024"] = dfm.peak_discharge_dfm_2024(out["F16"], out["H14"], out["XZ9"])
    return out


def _compute_breach_width(v: dict, missing: list[str]) -> dict[str, MethodResult]:
    out: dict[str, MethodResult] = {}

    if not ({"V_w", "h_b", "failure_mode"} & set(missing)):
        out["F95"] = f95.breach_width_f95(v["V_w"], v["h_b"], v["failure_mode"])
        out["F8"] = f8.breach_width_f8(v["V_w"], v["h_b"], v["failure_mode"])
    else:
        needed = [m for m in ("V_w", "h_b", "failure_mode") if m in missing]
        out["F95"] = _blocked_missing("F95", "m", needed)
        out["F8"] = _blocked_missing("F8", "m", needed)

    xz9_needed = {"V_w", "h_w", "h_b", "h_d", "dam_type", "failure_mode", "erodibility"}
    if not (xz9_needed & set(missing)):
        out["XZ9"] = xz9.breach_width_xz9(
            v["V_w"], v["h_w"], v["h_b"], v["h_d"], v["dam_type"],
            v["failure_mode"], v["erodibility"],
        )
    else:
        out["XZ9"] = _blocked_missing("XZ9", "m", [name for name in
            ("V_w", "h_w", "h_b", "h_d", "dam_type", "failure_mode", "erodibility")
            if name in missing])
    out["DFM_updated"] = dfm.breach_width_dfm_updated(out["F95"], out["F8"], out["XZ9"])
    return out


def _compute_failure_time(v: dict, missing: list[str]) -> dict[str, MethodResult]:
    out: dict[str, MethodResult] = {}

    if not ({"V_w", "h_b"} & set(missing)):
        out["F95"] = f95.failure_time_f95(v["V_w"], v["h_b"])
        out["F8"] = f8.failure_time_f8(v["V_w"], v["h_b"])
    else:
        needed = [m for m in ("V_w", "h_b") if m in missing]
        out["F95"] = _blocked_missing("F95", "s", needed)
        out["F8"] = _blocked_missing("F8", "s", needed)

    if not ({"V_w", "h_w"} & set(missing)):
        out["MCLM"] = mclm.failure_time_mclm(v["V_w"], v["h_w"])
    else:
        out["MCLM"] = _blocked_missing("MCLM", "s", [m for m in ("V_w", "h_w") if m in missing])

    out["DFM_updated"] = dfm.failure_time_dfm_updated(out["F95"], out["F8"], out["MCLM"])
    return out


_INPUT_NAMES = {
    "water_volume_above_invert": "V_w", "water_height_above_invert": "h_w", "breach_height": "h_b",
    "dam_height": "h_d", "average_embankment_width": "W_ave", "dam_type": "dam_type",
    "failure_mode": "failure_mode", "erodibility": "erodibility",
}


def placeholder_breach_inputs(dam: Dam) -> list[str]:
    """Equation-input symbols (V_w, h_w, ...) whose SourcedValue has `status: placeholder`,
    whether or not it carries a value."""
    return [sym for field, sym in _INPUT_NAMES.items()
            if getattr(dam.breach_inputs, field).status == "placeholder"]


def _imposed_range(range_value, output: str) -> dict:
    """Build an `OutputRange`-shaped dict from a `Dam.imposed_ranges` field (`RangeValue`:
    `value: [low, high] | None`), for a dam with `equations_applicable: false`
    (`docs/handoff_contract.md` §4.2: "Dams with `equations_applicable: false` return
    `imposed_ranges` from the config")."""
    unit = {"peak_discharge_m3s": "m3s", "breach_width_m": "m", "failure_time_s": "s"}[output]
    if range_value.value is None:
        return {"methods": {}, "low": None, "high": None, "unit": unit, "interval": "imposed",
                "selected_pair": None, "status": "blocked",
                "reason": f"imposed_ranges.{output} is a placeholder (status: placeholder)"}
    lo, hi = range_value.value
    return {"methods": {}, "low": lo, "high": hi, "unit": unit, "interval": "imposed",
            "selected_pair": None, "source": range_value.source}


def compute_dam(dam: Dam) -> dict:
    """Compute `breach_params.json`'s per-dam entry for one `Dam`.

    `equations_applicable: false` (loader-enforced for `kind: concrete_dam`,
    `docs/Equations.md` §7) skips the Azmi (2026) equations entirely and reports
    `dam.imposed_ranges` instead, flagged with caveat `concrete_dam_imposed`.
    """
    if not dam.equations_applicable:
        ir = dam.imposed_ranges
        warnings = ["concrete_dam_imposed", "failure_time_uncertain", "clear_water"]
        placeholders = [name for name, rv in (("peak_discharge_m3s", ir.peak_discharge_m3s),
                                               ("breach_width_m", ir.breach_width_m),
                                               ("failure_time_s", ir.failure_time_s))
                        if rv.value is None]
        if placeholders or any(rv.status == "placeholder" for rv in
                               (ir.peak_discharge_m3s, ir.breach_width_m, ir.failure_time_s)):
            warnings.append("placeholder_data")
        return {
            "dam_id": dam.id,
            "equations_applicable": False,
            "inputs_used": {"Vw_m3": None, "hw_m": None, "hb_m": None, "hd_m": None,
                             "Wave_m": None, "dam_type_code": None,
                             "failure_mode": None, "erodibility": None},
            "parameters": {
                "peak_discharge_m3s": _imposed_range(ir.peak_discharge_m3s, "peak_discharge_m3s"),
                "breach_width_m": _imposed_range(ir.breach_width_m, "breach_width_m"),
                "failure_time_s": _imposed_range(ir.failure_time_s, "failure_time_s"),
            },
            "warnings": warnings,
        }

    v, missing = _values(dam)
    # Feature 3: a placeholder with a number is still used in the equations, so it is flagged
    # per dam exactly like a null one (only nulls block a method).
    placeholder_inputs = placeholder_breach_inputs(dam)

    warnings: list[str] = []
    if v["h_b"] is not None and v["h_d"] is not None:
        warnings += warn_if_breach_exceeds_dam(v["h_b"], v["h_d"])
    if missing:
        warnings.append(f"placeholder input(s): {', '.join(missing)}")
    valued = [name for name in placeholder_inputs if name not in missing]
    if valued:
        warnings.append(f"placeholder-valued input(s) used: {', '.join(valued)}")

    qp = _compute_peak_discharge(v, missing)
    bw = _compute_breach_width(v, missing)
    tf = _compute_failure_time(v, missing)

    if qp["Z20"].is_blocked and "dam_type" not in missing and v["dam_type"] not in ("HD", "CD"):
        warnings.append("z20_dam_type_unmapped")

    warnings += ["failure_time_uncertain", "clear_water"]
    if dam.kind in ("moraine_dammed_lake", "landslide_dam"):
        warnings.append("moraine_extrapolation")
    if placeholder_inputs:
        warnings.append("placeholder_data")

    qp_range = ranges.method_range("peak_discharge_m3s", qp["DFM_updated"], qp["DFM_2024"])
    bw_range = ranges.method_range("breach_width_m", bw["DFM_updated"], bw["XZ9"])
    tf_range = ranges.method_range("failure_time_s", tf["DFM_updated"], tf["F8"])

    return {
        "dam_id": dam.id,
        "equations_applicable": True,
        "inputs_used": {
            "Vw_m3": v["V_w"], "hw_m": v["h_w"], "hb_m": v["h_b"], "hd_m": v["h_d"],
            "Wave_m": v["W_ave"], "dam_type_code": v["dam_type"],
            "failure_mode": v["failure_mode"], "erodibility": v["erodibility"],
        },
        "parameters": {
            "peak_discharge_m3s": {"methods": {k: r.to_dict() for k, r in qp.items()}, **qp_range.to_dict()},
            "breach_width_m": {"methods": {k: r.to_dict() for k, r in bw.items()}, **bw_range.to_dict()},
            "failure_time_s": {"methods": {k: r.to_dict() for k, r in tf.items()}, **tf_range.to_dict()},
        },
        "warnings": warnings,
    }


def compute_breach_params(cfg: SiteConfig) -> dict:
    """Build the full `breach_params.json` payload for a `SiteConfig`."""
    dam_entries = [compute_dam(dam) for dam in cfg.dams]

    placeholder_fields = [p for p in cfg.placeholder_fields
                          if any(tag in p for tag in (".breach_inputs.", ".imposed_ranges.", ".trigger."))]

    return {
        "contract_version": CONTRACT_VERSION,
        "site_id": cfg.site.id,
        "has_placeholders": bool(placeholder_fields),
        "placeholder_fields": placeholder_fields,
        "dams": dam_entries,
        "provenance": {"method": "empirical", "code_version": _code_version(),
                        "data_sources": ["docs/Equations.md", "docs/paper_azmi.md"]},
    }


def write_breach_params(cfg: SiteConfig, data_dir: Path | None = None, payload: dict | None = None) -> Path:
    """Compute (or take `payload`) and write `<data dir>/<site_id>/breach/breach_params.json`,
    validated against `contracts/schemas/breach_params.schema.json` first.

    Returns the path written.
    """
    from backend.m0_api import schemas  # local import: keep m2_breach importable without m0_api at module load

    payload = payload if payload is not None else compute_breach_params(cfg)
    schemas.validate("breach_params.schema.json", payload)

    out_dir = Path(data_dir or default_data_dir()) / cfg.site.id / "breach"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "breach_params.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    return out_path

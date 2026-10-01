"""Real `design/scenario_design.json` generator (docs/handoff_contract.md §4.3,
M5 -> M0, M3, M4) -- Feature 4 (docs/decisions.md 2026-10-01 "Feature 4").

Builds the maximin-LHS design for a site's target dam (`domains.far_field.inflow.from`) from
**Feature 3's own `breach/breach_params.json`** (never recomputed here; its SHA-256 is recorded).

Sampled inputs:
- `breach_width_m`, `failure_time_s`: always, over M2's pair range widened by
  `input_widen_fraction` (docs/m5_specs.md §2).
- `peak_discharge_m3s` (F4-D1): only when the target dam has no sourced weir method, so its
  hydrograph is `triangular` and needs Q_p -- sampled over M2's widened Q_p pair range. A blocked
  Q_p range (e.g. FD/ZD dams, Z20) then blocks the design, naming the dam and reason.
- `water_volume_m3` (F4-D5): sampled only over an explicit `emulator_inputs` range in the site
  config (contract §3.3; not widened); otherwise fixed at `breach_inputs.water_volume_above_invert`,
  recorded as fixed.

Every sampled point is turned into a real M2 hydrograph (`m2_breach.hydrograph.hydrograph_for_dam`)
before it is accepted; a point whose hydrograph cannot be built (e.g. an infeasible triangle) is
rejected and listed in `provenance.rejected` with its reason -- never repaired with an invented
value. IDs are assigned to accepted points only.

Stability (F4-D3): `provenance.fingerprint` hashes every input that determines the sampled values
(site, dam, raw ranges, fixed values, hydrograph method, settings, seed). Rewriting an identical
design is a no-op; a changed design archives the old file under `design/history/` and continues ID
numbering past every ID ever issued, so an ID is never reused for different parameters.
IDs follow the code-wide scheme (F4-D2): `<site>__s<NNN>`, `<site>__n_holdout<NNN>`,
`<site>__n_<slug>`, `<site>__hist_<event>`.

If a required range or input is blocked, `ScenarioDesignBlockedError` is raised rather than
inventing a range (CLAUDE.md rule 3).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from backend.m2_breach.breach_params import compute_dam
from backend.m5_emulator.inputs import DEFAULT_SCALING
from backend.m5_emulator.library import maximin_lhs, _widen
from backend.shared.site_config import Dam, SiteConfig, default_data_dir

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "m5_scenario_design.yaml"
CONTRACT_VERSION = "0.3.0"

#: Always-sampled inputs with an M2-computed range (`backend.m2_breach.ranges.PAIRS`).
RANGED_INPUTS = ("breach_width_m", "failure_time_s")
#: Sampling scale per input. Q_p spans orders of magnitude like volume -> log10.
SCALING = {**DEFAULT_SCALING, "peak_discharge_m3s": "log10"}

_UNIT = {"breach_width_m": "m", "failure_time_s": "s", "peak_discharge_m3s": "m3s", "water_volume_m3": "m3"}
_ID_RE = re.compile(r"__(s|n_holdout)(\d{3,})$")


class BreachParamsUnavailable(Exception):
    """`breach/breach_params.json` is missing, unreadable, or stale for the current config."""


class ScenarioDesignBlockedError(Exception):
    """A required M2 breach-parameter range (or the fixed water_volume_m3
    input) is blocked or missing. Raised instead of guessing a value."""

    def __init__(self, dam_id: str, param: str, reason: str):
        super().__init__(f"dam '{dam_id}': {param} is blocked: {reason}")
        self.dam_id = dam_id
        self.param = param
        self.reason = reason


@dataclass(frozen=True)
class ScenarioDesignSettings:
    n: int = 30
    n_holdout: int = 5
    seed: int = 42
    input_widen_fraction: float = 0.20
    method: str = "maximin_lhs"

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError(f"n must be >= 1, got {self.n}")
        if self.n_holdout < 0:
            raise ValueError(f"n_holdout must be >= 0, got {self.n_holdout}")
        if not 0.0 <= self.input_widen_fraction < 1.0:
            raise ValueError(f"input_widen_fraction must be in [0, 1), got {self.input_widen_fraction}")
        if self.method != "maximin_lhs":
            raise ValueError(f"unknown method {self.method!r}")


def load_scenario_design_settings(path: str | Path | None = None, **overrides) -> ScenarioDesignSettings:
    """Load `config/m5_scenario_design.yaml` (or `path`), applying any keyword overrides on top."""
    path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {f.name for f in fields(ScenarioDesignSettings)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    settings = ScenarioDesignSettings(**raw)
    return replace(settings, **overrides) if overrides else settings


def load_breach_params(site_id: str, data_dir: Path) -> tuple[dict, str]:
    """(payload, sha256) of Feature 3's `breach/breach_params.json`."""
    path = Path(data_dir) / site_id / "breach" / "breach_params.json"
    if not path.is_file():
        raise BreachParamsUnavailable(f"{path} is missing -- run the breach stage (Feature 3) before the design")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise BreachParamsUnavailable(f"{path} is not valid JSON: {e}") from None
    if payload.get("site_id") != site_id:
        raise BreachParamsUnavailable(f"{path} is for site {payload.get('site_id')!r}, not {site_id!r}")
    return payload, hashlib.sha256(raw).hexdigest()


def _breach_entry(breach_params: dict, dam: Dam) -> dict:
    """The dam's entry, checked against the *current* config so a stale file is never used."""
    entry = next((d for d in breach_params.get("dams", []) if d.get("dam_id") == dam.id), None)
    if entry is None:
        raise BreachParamsUnavailable(f"breach_params.json has no entry for dam '{dam.id}'")
    current = compute_dam(dam)["inputs_used"]  # the inputs only -- ranges are taken from the file
    if entry.get("inputs_used") != current:
        raise BreachParamsUnavailable(
            f"breach_params.json is stale for dam '{dam.id}': it was computed from {entry.get('inputs_used')} "
            f"but the site config now gives {current} -- re-run the breach stage")
    return entry


def _m2_ranges(dam: Dam, entry: dict | None = None) -> dict[str, tuple[float, float]]:
    """M2's low/high pair range for each of `RANGED_INPUTS`, from a breach_params dam entry
    (or, for callers without the file, `compute_dam`). Raises `ScenarioDesignBlockedError`
    naming the first blocked input."""
    entry = entry if entry is not None else compute_dam(dam)
    ranges: dict[str, tuple[float, float]] = {}
    for name in RANGED_INPUTS:
        param = entry["parameters"][name]
        if param.get("status") == "blocked":
            raise ScenarioDesignBlockedError(dam.id, name, param["reason"])
        ranges[name] = (param["low"], param["high"])
    return ranges


def _scenario_id(site_id: str, kind: str, index: int) -> str:
    """docs/decisions.md "ID naming scheme". `held_out` isn't one of the four
    documented kinds (design | demo | historical | named extra) -- it's
    written using the `__n_<slug>` named-extra shape, `kind: "held_out"`
    distinguishing it in the payload (this module's own choice)."""
    if kind == "design":
        return f"{site_id}__s{index:03d}"
    if kind == "held_out":
        return f"{site_id}__n_holdout{index:03d}"
    raise ValueError(f"unknown kind {kind!r}")


def _scale(name: str, unit_value: float, lo: float, hi: float) -> float:
    if SCALING.get(name) == "log10":
        return float(10 ** (np.log10(lo) + unit_value * (np.log10(hi) - np.log10(lo))))
    return float(lo + unit_value * (hi - lo))


def design_from_ranges(site_id: str, dam_id: str, water_volume_m3: float,
                        ranges: dict[str, tuple[float, float]], caveats: list[str],
                        settings: ScenarioDesignSettings, *,
                        peak_discharge_range: tuple[float, float] | None = None,
                        water_volume_range: tuple[float, float] | None = None,
                        first_index: int = 1, first_holdout_index: int = 1) -> dict:
    """Build the contract's `design/scenario_design.json` payload (§4.3) from already-resolved
    M2 `ranges` (raw pair ranges, not yet widened). Pure function (no hydrograph check, no IDs
    history): `build_scenario_design` adds those. `peak_discharge_range` (raw M2 pair, widened like
    the others) and `water_volume_range` (explicit config range, not widened) add sampled
    dimensions; without them the design is exactly the 2-input design."""
    dims: list[tuple[str, tuple[float, float]]] = [
        (name, _widen(*ranges[name], SCALING[name], settings.input_widen_fraction)) for name in RANGED_INPUTS]
    if peak_discharge_range is not None:
        dims.append(("peak_discharge_m3s", _widen(*peak_discharge_range, SCALING["peak_discharge_m3s"],
                                                 settings.input_widen_fraction)))
    if water_volume_range is not None:
        dims.append(("water_volume_m3", tuple(float(v) for v in water_volume_range)))

    def _params(row: np.ndarray) -> dict[str, float]:
        params = {"water_volume_m3": water_volume_m3}
        for j, (name, (lo, hi)) in enumerate(dims):
            params[name] = _scale(name, row[j], lo, hi)
        return params

    def _make(n: int, kind: str, seed: int, start: int) -> list[dict]:
        if n == 0:
            return []
        unit = maximin_lhs(n, len(dims), seed=seed)
        return [{"scenario_id": _scenario_id(site_id, kind, start + i), "kind": kind, "params": _params(unit[i])}
                for i in range(n)]

    return {
        "contract_version": CONTRACT_VERSION,
        "site_id": site_id,
        "model": "delft3d",
        "method": settings.method,
        "seed": settings.seed,
        "n": settings.n,
        "inputs": [{"name": name, "dam_id": dam_id, "low": lo, "high": hi, "unit": _UNIT[name]} for name, (lo, hi) in dims],
        "scenarios": _make(settings.n, "design", settings.seed, first_index),
        "extra": _make(settings.n_holdout, "held_out", settings.seed + 1, first_holdout_index),
        "has_placeholders": True,
        "caveats": caveats,
    }


def _volume_range(cfg: SiteConfig, dam: Dam) -> tuple[tuple[float, float] | None, dict]:
    """(range, provenance) of an explicit `emulator_inputs` water_volume_m3 range for `dam`."""
    for item in cfg.emulator_inputs:
        if item.name == "water_volume_m3" and item.dam_id == dam.id:
            if item.range.low is None or item.range.high is None:
                raise ScenarioDesignBlockedError(dam.id, "water_volume_m3",
                                                 "emulator_inputs water_volume_m3 range has a null bound (placeholder)")
            return (item.range.low, item.range.high), {
                "status": "sampled", "low": item.range.low, "high": item.range.high, "scaling": SCALING["water_volume_m3"],
                "source": item.range.source, "range_status": item.range.status, "basis": item.range.basis,
                "widened": False}
    return None, {"status": "fixed", "value": dam.breach_inputs.water_volume_above_invert.value,
                  "source": "breach_inputs.water_volume_above_invert", "value_status": dam.breach_inputs.water_volume_above_invert.status,
                  "reason": "no emulator_inputs water_volume_m3 range in the site config (F4-D5)"}


def fingerprint(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _issued_indices(design_dir: Path) -> tuple[int, int]:
    """Highest design and held-out index ever issued (current design + history)."""
    top = {"s": 0, "n_holdout": 0}
    files = [design_dir / "scenario_design.json", *sorted((design_dir / "history").glob("*.json"))]
    for path in files:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for item in payload.get("scenarios", []) + payload.get("extra", []):
            m = _ID_RE.search(item.get("scenario_id", ""))
            if m:
                top[m.group(1)] = max(top[m.group(1)], int(m.group(2)))
    return top["s"], top["n_holdout"]


def build_scenario_design(cfg: SiteConfig, dam_id: str,
                           settings: ScenarioDesignSettings | None = None,
                           data_dir: Path | None = None, *, first_index: int = 1,
                           first_holdout_index: int = 1) -> dict:
    """Build the contract's `design/scenario_design.json` payload (§4.3) for `dam_id` from Feature
    3's `breach_params.json` in `data_dir`, with every accepted scenario hydrograph-checked.

    Raises `ScenarioDesignBlockedError` (a required input/range is blocked, or no sampled point
    produces a hydrograph) or `BreachParamsUnavailable` (missing/stale breach output).
    """
    from backend.m2_breach.hydrograph import HydrographBlocked, _weir_prereqs, hydrograph_for_dam

    settings = settings or load_scenario_design_settings()
    data_dir = Path(data_dir or default_data_dir())
    dam = next((d for d in cfg.dams if d.id == dam_id), None)
    if dam is None:
        raise ValueError(f"no dam '{dam_id}' in site '{cfg.site.id}'")

    water_volume_m3 = dam.breach_inputs.water_volume_above_invert.value
    if water_volume_m3 is None:
        raise ScenarioDesignBlockedError(
            dam_id, "water_volume_m3",
            "water_volume_above_invert is a placeholder (status: placeholder) "
            "-- cannot fix a design value",
        )

    breach_params, breach_sha = load_breach_params(cfg.site.id, data_dir)
    entry = _breach_entry(breach_params, dam)
    m2_ranges = _m2_ranges(dam, entry)

    curve, _, weir_reason = _weir_prereqs(dam)
    hydrograph_method = "breach_growth_weir" if curve is not None else "triangular"
    qp_range = None
    if hydrograph_method == "triangular":
        qp = entry["parameters"]["peak_discharge_m3s"]
        if qp.get("status") == "blocked":
            raise ScenarioDesignBlockedError(
                dam_id, "peak_discharge_m3s",
                f"the triangular hydrograph needs Q_p (weir method unavailable: {weir_reason}) but M2's "
                f"peak-discharge range is blocked: {qp['reason']}")
        qp_range = (qp["low"], qp["high"])
    volume_range, volume_prov = _volume_range(cfg, dam)

    caveats = ["moraine_extrapolation"] if dam.kind in ("moraine_dammed_lake", "landslide_dam") else []
    if qp_range is not None:
        caveats.append("peak_discharge_sampled_independently")

    spec = {
        "site_id": cfg.site.id, "dam_id": dam_id, "settings": asdict(settings),
        "raw_ranges": {**{k: list(v) for k, v in m2_ranges.items()},
                       **({"peak_discharge_m3s": list(qp_range)} if qp_range else {})},
        "water_volume": volume_prov, "hydrograph_method": hydrograph_method,
    }
    fp = fingerprint(spec)

    payload = design_from_ranges(cfg.site.id, dam_id, float(water_volume_m3), m2_ranges, caveats, settings,
                                 peak_discharge_range=qp_range, water_volume_range=volume_range,
                                 first_index=first_index, first_holdout_index=first_holdout_index)
    payload["has_placeholders"] = bool(cfg.placeholder_fields) or bool(breach_params.get("has_placeholders"))

    # Hydrograph check: keep only points that produce a real M2 hydrograph; renumber accepted ones.
    accepted, rejected, checks = {"scenarios": [], "extra": []}, [], []
    counters = {"scenarios": first_index, "extra": first_holdout_index}
    for key, kind in (("scenarios", "design"), ("extra", "held_out")):
        for item in payload[key]:
            try:
                hg = hydrograph_for_dam(dam, item["params"])
            except (HydrographBlocked, ValueError) as e:
                rejected.append({"kind": kind, "params": item["params"], "dam_id": dam_id,
                                 "reason": str(getattr(e, "reason", e))})
                continue
            sid = _scenario_id(cfg.site.id, kind, counters[key])
            counters[key] += 1
            accepted[key].append({**item, "scenario_id": sid})
            checks.append({"scenario_id": sid, "hydrograph_method": hg.method, "peak_q_m3s": hg.peak_q_m3s,
                           "volume_m3": hg.volume_m3, "mass_balance_error_pct": hg.mass_balance_error_pct})
    if not accepted["scenarios"]:
        raise ScenarioDesignBlockedError(dam_id, "hydrograph",
                                         f"none of the {settings.n} sampled points produced a hydrograph: "
                                         + "; ".join(sorted({r['reason'] for r in rejected})))
    payload["scenarios"], payload["extra"] = accepted["scenarios"], accepted["extra"]
    if rejected:
        payload["caveats"] = [*payload["caveats"], "design_points_rejected"]
    widened = {i["name"]: [i["low"], i["high"]] for i in payload["inputs"]}
    payload["provenance"] = {
        "fingerprint": fp,
        "breach_params_sha256": breach_sha,
        "breach_params_path": f"breach/breach_params.json",
        "target_dam": dam_id,
        "method": settings.method, "seed": settings.seed, "holdout_seed": settings.seed + 1,
        "n_requested": settings.n, "n_holdout_requested": settings.n_holdout,
        "n_accepted": len(accepted["scenarios"]), "n_holdout_accepted": len(accepted["extra"]),
        "input_widen_fraction": settings.input_widen_fraction,
        "raw_m2_ranges": spec["raw_ranges"], "widened_ranges": widened, "scaling": {k: SCALING[k] for k in widened},
        "sampled_inputs": [i["name"] for i in payload["inputs"]],
        "fixed_inputs": ({"water_volume_m3": volume_prov} if volume_range is None else {}),
        "water_volume": volume_prov,
        "hydrograph_method": hydrograph_method,
        "weir_unavailable_reason": weir_reason,
        "scenario_checks": checks, "rejected": rejected,
        "placeholder_fields": sorted(cfg.placeholder_fields),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return payload


def _design_dir(site_id: str, data_dir: Path) -> Path:
    return Path(data_dir) / site_id / "design"


def load_design(site_id: str, data_dir: Path | None = None) -> dict | None:
    path = _design_dir(site_id, Path(data_dir or default_data_dir())) / "scenario_design.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def write_scenario_design(cfg: SiteConfig, dam_id: str, data_dir: Path | None = None,
                           settings: ScenarioDesignSettings | None = None) -> Path:
    """Compute and write `<data dir>/<site_id>/design/scenario_design.json` (schema-validated).

    Identical fingerprint -> no-op (the file, its IDs and timestamp are kept). Changed design ->
    the old file is archived in `design/history/<fingerprint>.json` and new IDs continue past every
    ID ever issued. Saved named scenarios (`design/named_scenarios.json`) are carried into `extra`.
    """
    from backend.m0_api import schemas  # local import: keep m5_emulator importable without m0_api at module load
    from backend.m5_emulator import whatif

    data_dir = Path(data_dir or default_data_dir())
    design_dir = _design_dir(cfg.site.id, data_dir)
    out_path = design_dir / "scenario_design.json"
    current = load_design(cfg.site.id, data_dir)

    probe = build_scenario_design(cfg, dam_id, settings, data_dir)
    if current is not None and (current.get("provenance") or {}).get("fingerprint") == probe["provenance"]["fingerprint"]:
        return out_path  # identical regeneration: no-op
    if current is not None:
        history = design_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        old_fp = (current.get("provenance") or {}).get("fingerprint") or hashlib.sha256(out_path.read_bytes()).hexdigest()
        (history / f"{old_fp}.json").write_text(json.dumps(current, indent=2), encoding="utf-8")
    top_s, top_h = _issued_indices(design_dir)
    payload = probe if (top_s, top_h) == (0, 0) else build_scenario_design(
        cfg, dam_id, settings, data_dir, first_index=top_s + 1, first_holdout_index=top_h + 1)
    payload["extra"] = payload["extra"] + whatif.load_named(cfg.site.id, data_dir)
    schemas.validate("scenario_design.schema.json", payload)
    design_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path

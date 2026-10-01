"""Feature 4: breach-level what-if (docs/decisions.md 2026-10-01, F4-D4).

`POST /sites/{id}/whatif`: override breach inputs and/or scenario parameters for one dam ->
validate -> recompute M2 for real (`m2_breach.breach_params.compute_dam`) -> build a real M2
hydrograph (`m2_breach.hydrograph.hydrograph_for_dam`) -> return it, optionally saved as a stable
named scenario `<site_id>__n_<slug>` (`design/named_scenarios.json`, carried into the design's
`extra` so Feature 5's campaign can run it). Flood maps are Feature 8 and not produced here.

Scenario parameters not given explicitly come from M2's own range bound for the chosen `case`
(`low`: B_ave low, T_f high, Q_p low; `high`: B_ave high, T_f low, Q_p high -- the same rule as
Feature 3's reference hydrographs). Overridden breach inputs are recorded as
`status: placeholder` ("what-if override"), so every result built from them is flagged. A
parameter M2 cannot provide (a blocked range) yields `status: "blocked"` with the reason --
never a guessed number.
"""

from __future__ import annotations

import fcntl
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.m2_breach.breach_params import compute_dam
from backend.m2_breach.hydrograph import HydrographBlocked, _weir_prereqs, hydrograph_for_dam
from backend.shared.site_config import Dam, SiteConfig, default_data_dir

CONTRACT_VERSION = "0.3.0"
NAMED_FILE = "named_scenarios.json"
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
RESERVED_SLUG_RE = re.compile(r"^(holdout\d+|m2_low|m2_high)$")
OVERRIDE_SOURCE = "what-if override (operator, unverified)"

#: what-if override name -> (breach_inputs field, unit)
INPUT_FIELDS = {
    "water_volume_m3": ("water_volume_above_invert", "m^3"),
    "water_height_m": ("water_height_above_invert", "m"),
    "breach_height_m": ("breach_height", "m"),
    "dam_height_m": ("dam_height", "m"),
    "average_embankment_width_m": ("average_embankment_width", "m"),
    "dam_type": ("dam_type", "enum"),
    "failure_mode": ("failure_mode", "enum"),
    "erodibility": ("erodibility", "enum"),
}
CASES = {"low": {"breach_width_m": "low", "failure_time_s": "high", "peak_discharge_m3s": "low"},
         "high": {"breach_width_m": "high", "failure_time_s": "low", "peak_discharge_m3s": "high"}}


class WhatIfInvalid(ValueError):
    """The request is malformed or names something that does not exist (HTTP 422)."""


class NameTaken(ValueError):
    """`save_as` is already used by a named scenario (HTTP 409)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputOverrides(_Strict):
    water_volume_m3: float | None = Field(default=None, gt=0)
    water_height_m: float | None = Field(default=None, gt=0)
    breach_height_m: float | None = Field(default=None, gt=0)
    dam_height_m: float | None = Field(default=None, gt=0)
    average_embankment_width_m: float | None = Field(default=None, gt=0)
    dam_type: Literal["HD", "CD", "FD", "ZD"] | None = None
    failure_mode: Literal["O", "P"] | None = None
    erodibility: Literal["H", "M", "L"] | None = None


class ScenarioOverrides(_Strict):
    breach_width_m: float | None = Field(default=None, gt=0)
    failure_time_s: float | None = Field(default=None, gt=0)
    peak_discharge_m3s: float | None = Field(default=None, gt=0)


class WhatIfRequest(_Strict):
    dam_id: str | None = None
    inputs: InputOverrides = InputOverrides()
    scenario: ScenarioOverrides = ScenarioOverrides()
    case: Literal["low", "high"] = "high"
    dt_s: float = Field(default=10.0, gt=0, le=3600)
    save_as: str | None = None


def parse_request(body: dict) -> WhatIfRequest:
    try:
        req = WhatIfRequest.model_validate(body)
    except ValidationError as e:
        errors = [{"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
        raise WhatIfInvalid("; ".join(f"{x['field']}: {x['message']}" for x in errors)) from None
    if req.save_as is not None and (not SLUG_RE.match(req.save_as) or RESERVED_SLUG_RE.match(req.save_as)):
        raise WhatIfInvalid(f"save_as '{req.save_as}' must match {SLUG_RE.pattern} and not be a reserved name")
    return req


# --- named scenarios -----------------------------------------------------------------------------
def _named_path(site_id: str, data_dir: Path) -> Path:
    return Path(data_dir) / site_id / "design" / NAMED_FILE


def load_named_records(site_id: str, data_dir: Path | None = None) -> list[dict]:
    path = _named_path(site_id, Path(data_dir or default_data_dir()))
    return json.loads(path.read_text(encoding="utf-8")).get("scenarios", []) if path.is_file() else []


def load_named(site_id: str, data_dir: Path | None = None) -> list[dict]:
    """Saved what-if scenarios as contract §4.3 ScenarioPoints (kind `named`)."""
    return [{"scenario_id": r["scenario_id"], "kind": "named", "params": r["params"], "source": r["source"]}
            for r in load_named_records(site_id, data_dir)]


def _save(cfg: SiteConfig, data_dir: Path, slug: str, params: dict, record: dict) -> str:
    from backend.m0_api import schemas
    from backend.m5_emulator import scenario_design

    scenario_id = f"{cfg.site.id}__n_{slug}"
    path = _named_path(cfg.site.id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".named.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _save_locked(cfg, data_dir, slug, params, record, scenario_id, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save_locked(cfg: SiteConfig, data_dir: Path, slug: str, params: dict, record: dict,
                 scenario_id: str, path: Path) -> str:
    from backend.m0_api import schemas
    from backend.m5_emulator import scenario_design

    records = load_named_records(cfg.site.id, data_dir)
    design = scenario_design.load_design(cfg.site.id, data_dir)
    taken = {r["scenario_id"] for r in records}
    if design:
        taken |= {i["scenario_id"] for i in design.get("scenarios", []) + design.get("extra", [])}
    if scenario_id in taken:
        raise NameTaken(f"scenario '{scenario_id}' already exists; choose another save_as name")
    point = {"scenario_id": scenario_id, "kind": "named", "params": params,
             "source": f"what-if saved {record['created_at']}"}
    schemas.validate("scenario_design.schema.json", {  # the point must be a valid design ScenarioPoint
        "contract_version": CONTRACT_VERSION, "site_id": cfg.site.id, "model": "delft3d", "method": "maximin_lhs",
        "seed": 0, "n": 1, "inputs": [], "scenarios": [], "extra": [point], "has_placeholders": True, "caveats": []})
    records.append({**point, **record})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"contract_version": CONTRACT_VERSION, "site_id": cfg.site.id, "scenarios": records},
                              indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    if design is not None:  # carry into the current design's extra so the campaign can select it
        design["extra"] = [*design.get("extra", []), point]
        schemas.validate("scenario_design.schema.json", design)
        (Path(data_dir) / cfg.site.id / "design" / "scenario_design.json").write_text(json.dumps(design, indent=2),
                                                                                       encoding="utf-8")
    return scenario_id


# --- evaluation ------------------------------------------------------------------------------------
def _apply_overrides(dam: Dam, inputs: InputOverrides) -> tuple[Dam, dict]:
    changed = {}
    bi = dam.breach_inputs
    updates = {}
    for name, (field, _unit) in INPUT_FIELDS.items():
        value = getattr(inputs, name)
        if value is None:
            continue
        current = getattr(bi, field)
        changed[name] = {"from": current.value, "to": value, "from_status": current.status}
        updates[field] = current.model_copy(update={"value": value, "source": OVERRIDE_SOURCE, "status": "placeholder"})
    if not updates:
        return dam, changed
    return dam.model_copy(update={"breach_inputs": bi.model_copy(update=updates)}), changed


def evaluate(cfg: SiteConfig, body: dict, data_dir: Path | None = None) -> dict:
    """Run one what-if. Raises WhatIfInvalid (422) or NameTaken (409)."""
    req = parse_request(body)
    data_dir = Path(data_dir or default_data_dir())
    dam_id = req.dam_id or cfg.domains.far_field.inflow.from_
    if dam_id == "far_field":
        dam_id = cfg.dams[0].id
    base_dam = next((d for d in cfg.dams if d.id == dam_id), None)
    if base_dam is None:
        raise WhatIfInvalid(f"dam_id '{dam_id}' is not a dam of site '{cfg.site.id}'")
    dam, changed = _apply_overrides(base_dam, req.inputs)
    entry = compute_dam(dam)  # real M2 recomputation with the overrides
    response = {"contract_version": CONTRACT_VERSION, "site_id": cfg.site.id, "dam_id": dam_id, "case": req.case,
                "input_overrides": changed, "breach_params": entry,
                "has_placeholders": bool(changed) or bool(cfg.placeholder_fields) or "placeholder_data" in entry["warnings"],
                "caveats": [w for w in entry["warnings"] if " " not in w]}

    pick, rng = CASES[req.case], entry["parameters"]
    volume = dam.breach_inputs.water_volume_above_invert.value
    blocked = []
    if volume is None:
        blocked.append("water_volume_m3: breach_inputs.water_volume_above_invert is a placeholder (null)")
    params: dict = {"water_volume_m3": float(volume) if volume is not None else None}
    basis = {"water_volume_m3": "override" if "water_volume_m3" in changed else "site config"}
    for name in ("breach_width_m", "failure_time_s"):
        given = getattr(req.scenario, name)
        if given is not None:
            params[name], basis[name] = given, "override"
        elif rng[name].get("status") == "blocked":
            blocked.append(f"{name}: M2 range is blocked: {rng[name].get('reason')}")
        else:
            params[name], basis[name] = float(rng[name][pick[name]]), f"M2 range {pick[name]} ({req.case} case)"
    curve, _, weir_reason = _weir_prereqs(dam)
    method = "breach_growth_weir" if curve is not None else "triangular"
    if method == "triangular":
        given = req.scenario.peak_discharge_m3s
        qp = rng["peak_discharge_m3s"]
        if given is not None:
            params["peak_discharge_m3s"], basis["peak_discharge_m3s"] = given, "override"
        elif qp.get("status") == "blocked":
            blocked.append(f"peak_discharge_m3s: needed by the triangular hydrograph (weir unavailable: {weir_reason}) "
                           f"but M2's range is blocked: {qp.get('reason')}")
        else:
            params["peak_discharge_m3s"] = float(qp[pick["peak_discharge_m3s"]])
            basis["peak_discharge_m3s"] = f"M2 range {pick['peak_discharge_m3s']} ({req.case} case)"
    response.update({"hydrograph_method": method, "scenario_params": params, "param_basis": basis})

    if not blocked:
        try:
            hg = hydrograph_for_dam(dam, {**params, "dt_s": req.dt_s})
        except (HydrographBlocked, ValueError) as e:
            blocked.append(f"hydrograph: {getattr(e, 'reason', e)}")
    if blocked:
        response.update({"status": "blocked", "blocked_reasons": blocked, "hydrograph": None, "saved_scenario_id": None})
        if req.save_as is not None:
            raise WhatIfInvalid("a blocked what-if cannot be saved: " + "; ".join(blocked))
        return response

    response.update({"status": "ok", "blocked_reasons": [], "hydrograph": {
        "method": hg.method, "dt_s": hg.dt_s, "t_s": [float(t) for t in hg.t_s], "q_m3s": [float(q) for q in hg.q_m3s],
        "peak_q_m3s": hg.peak_q_m3s, "time_to_peak_s": hg.time_to_peak_s, "volume_m3": hg.volume_m3,
        "mass_balance_error_pct": hg.mass_balance_error_pct, "peak_within_m2_range": hg.peak_within_m2_range,
        "caveats": hg.caveats, "has_placeholders": hg.has_placeholders}})
    response["has_placeholders"] = response["has_placeholders"] or hg.has_placeholders

    saved = None
    if req.save_as is not None:
        # A saved scenario is run later with the *configured* dam (Feature 5): it may only be saved if
        # its parameters alone reproduce this hydrograph.
        replay = hydrograph_for_dam(base_dam, {**params, "dt_s": req.dt_s})
        if abs(replay.peak_q_m3s - hg.peak_q_m3s) > 1e-9 * max(1.0, hg.peak_q_m3s) or \
                abs(replay.volume_m3 - hg.volume_m3) > 1e-9 * max(1.0, hg.volume_m3):
            raise WhatIfInvalid("this what-if changes breach inputs that are not scenario parameters "
                                f"({', '.join(sorted(set(changed) - {'water_volume_m3'}))}); its hydrograph cannot be "
                                "reproduced from the scenario parameters alone, so it cannot be saved")
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        saved = _save(cfg, data_dir, req.save_as, params, {
            "created_at": created, "dam_id": dam_id, "case": req.case, "input_overrides": changed,
            "param_basis": basis, "hydrograph_method": method, "peak_q_m3s": hg.peak_q_m3s})
    response["saved_scenario_id"] = saved
    return response

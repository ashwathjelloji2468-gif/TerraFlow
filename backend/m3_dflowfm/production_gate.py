"""Feature 13, phase 1: production-readiness gate for a real D-Flow FM campaign.

Answers one question honestly: may this site's real (production) D-Flow FM campaign start? It never
supplies or infers a value. Every required input is reported with one of:

- ``READY``            -- sourced value / present artifact / approved gate.
- ``PLACEHOLDER``      -- a value exists but is explicitly a placeholder (blocking in production).
- ``BLOCKED``          -- value null, artifact missing, gate unavailable, or a contract decision pending.
- ``CONTROLLED_PASS``  -- a controlled (non-production) check passed; informational, never READY.

The overall ``verdict`` is ``READY`` only when every blocking item is ``READY``. A placeholder is
never downgraded to a warning here (unlike the plain preflight, which only labels results).

Inputs: the site config (``sites/<id>.yaml`` or the onboarded copy), the site's terrain folder,
and ``docs/real_input_checklist.json`` (the team's production-gate record).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backend.shared.site_config import SiteConfig, SourcedValue, default_data_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKLIST_PATH = REPO_ROOT / "docs" / "real_input_checklist.json"

READY, PLACEHOLDER, BLOCKED, CONTROLLED_PASS = "READY", "PLACEHOLDER", "BLOCKED", "CONTROLLED_PASS"
FAILED_ACCEPTANCE = "FAILED_ACCEPTANCE"  # run-level status (acceptance.py); listed for the API enum

#: Site-config paths that a far-field D-Flow FM production run does not consume. Everything else in
#: the config (CRS, far-field domain/inflow, dams incl. cascade triggers, POIs) is required.
NON_BLOCKING_PREFIXES = {
    "delft3d": ("domains.near_field", "events", "simulation"),
    "sph": ("events", "simulation"),
}
#: Checklist keys that only apply to the SPH (near-field) side of the pipeline.
SPH_ONLY_CHECKLIST_KEYS = {"near_field_domain_and_resolution", "m3_to_m4_routed_section", "m4_campaign_launcher"}
CASCADE_CHECKLIST_KEYS = {"teesta_iii_cascade_trigger_threshold", "cascade_trigger_representation",
                          "cascade_trigger_contract"}
TERRAIN_FILES = ("dem.tif", "roughness.tif", "domain.gpkg", "pois.gpkg", "centreline.gpkg",
                 "grid.json", "domain_mask.tif")


def _item(key: str, status: str, *, blocking: bool, source: str | None = None, detail: str = "",
          category: str = "site_config") -> dict:
    return {"key": key, "category": category, "status": status, "blocking": blocking,
            "source": source, "detail": detail}


def _walk(value, path: str = ""):
    from pydantic import BaseModel
    if isinstance(value, SourcedValue):
        yield path, value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name), f"{path}.{name}" if path else name)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _walk(item, f"{path}[{i}]")


def config_items(cfg: SiteConfig, model: str = "delft3d") -> list[dict]:
    skip = NON_BLOCKING_PREFIXES.get(model, ())
    items = []
    for path, sv in _walk(cfg):
        blocking = not any(path == p or path.startswith(p + ".") or path.startswith(p + "[") for p in skip)
        if sv.value is None:
            status, detail = BLOCKED, "value is null"
        elif sv.status == "sourced":
            status, detail = READY, ""
        else:
            status, detail = PLACEHOLDER, "value is a placeholder, not a production input"
        items.append(_item(path, status, blocking=blocking, source=sv.source, detail=detail))
    if not cfg.points_of_interest:
        items.append(_item("points_of_interest", BLOCKED, blocking=True, detail="no POIs configured"))
    return items


def terrain_item(site_id: str, data_dir: Path) -> dict:
    terrain = data_dir / site_id / "terrain"
    missing = [n for n in TERRAIN_FILES if not (terrain / n).is_file()]
    if missing:
        return _item("terrain_artifacts", BLOCKED, blocking=True, category="artifact", source=f"data/{site_id}/terrain",
                     detail="missing: " + ", ".join(missing))
    return _item("terrain_artifacts", READY, blocking=True, category="artifact", source=f"data/{site_id}/terrain",
                 detail=f"all {len(TERRAIN_FILES)} M1 terrain products present (presence only; approval is a checklist item)")


_CHECKLIST_STATUS = {
    "READY": READY, "CONTROLLED PASS": CONTROLLED_PASS, "PLACEHOLDER": PLACEHOLDER,
    "UNAVAILABLE — PRODUCTION RUN BLOCKED": BLOCKED, "CONTRACT DECISION REQUIRED": BLOCKED,
}


def _checklist_status(raw: str | None) -> str:
    return _CHECKLIST_STATUS.get(raw or "", BLOCKED)  # unknown/missing status is never READY


def checklist_items(checklist: dict, site_id: str, model: str, has_cascade: bool) -> list[dict]:
    items = []
    for gate in checklist.get("shared_gates", []):
        key, status = gate["key"], _checklist_status(gate.get("status"))
        applies = not (model == "delft3d" and key in SPH_ONLY_CHECKLIST_KEYS) and \
            not (key in CASCADE_CHECKLIST_KEYS and not has_cascade)
        blocking = applies and status != CONTROLLED_PASS
        items.append(_item(key, status, blocking=blocking, category="shared_gate", source=gate.get("source"),
                           detail=f"{gate.get('status')}: {gate.get('note', '')}".strip()))
    site = (checklist.get("sites") or {}).get(site_id)
    if site is not None:
        gate_status = site.get("production_gate")
        items.append(_item("site_production_gate", READY if gate_status == "READY" else BLOCKED, blocking=True,
                           category="checklist", source="docs/real_input_checklist.json",
                           detail=f"checklist production_gate = {gate_status}"))
        for entry in site.get("inputs", []):
            key, status = entry["key"], _checklist_status(entry.get("status"))
            applies = not (model == "delft3d" and key in SPH_ONLY_CHECKLIST_KEYS) and \
                not (key in CASCADE_CHECKLIST_KEYS and not has_cascade)
            items.append(_item(key, status, blocking=applies and status != CONTROLLED_PASS, category="checklist",
                               source=entry.get("source"), detail=f"{entry.get('status')}: {entry.get('note', '')}".strip()))
    return items


def evaluate(site_id: str, *, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
             checklist_path: str | Path | None = CHECKLIST_PATH, model: str = "delft3d") -> dict:
    """Production-readiness report for `site_id` (see module docstring)."""
    from backend.m3_dflowfm.preflight import _load_config
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    report = {"site_id": site_id, "model": model, "mode": "production",
              "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "verdict": BLOCKED, "items": [], "blocking": [], "config_loaded": False}
    try:
        cfg, _ = _load_config(site_id, data_dir, Path(sites_dir) if sites_dir else None)
    except Exception as e:  # unknown / unconfigured site: honest, never READY
        report["items"] = [_item("site_config", BLOCKED, blocking=True, detail=f"cannot load a site config for '{site_id}' ({type(e).__name__})")]
        report["blocking"] = ["site_config"]
        return report
    report["config_loaded"] = True
    has_cascade = any(d.trigger is not None or d.triggered_by for d in cfg.dams)
    checklist = {}
    if checklist_path is not None and Path(checklist_path).is_file():
        checklist = json.loads(Path(checklist_path).read_text(encoding="utf-8"))
    elif checklist_path is not None:
        report["items"].append(_item("production_checklist", BLOCKED, blocking=True, category="checklist",
                                     detail="production checklist (docs/real_input_checklist.json) not found"))
    report["items"] += config_items(cfg, model) + [terrain_item(site_id, data_dir)] + \
        checklist_items(checklist, site_id, model, has_cascade)
    report["blocking"] = [i["key"] for i in report["items"] if i["blocking"] and i["status"] != READY]
    report["verdict"] = READY if not report["blocking"] else BLOCKED
    report["counts"] = {s: sum(1 for i in report["items"] if i["status"] == s)
                        for s in (READY, PLACEHOLDER, BLOCKED, CONTROLLED_PASS)}
    return report

"""Feature 17 (decision E7): where the M3 -> M4 routing section comes from, and whether it may be used
in production.

Reviewed sections live OUTSIDE the site-config schema, in `config/routing_sections/<site_id>.yaml`
(format: `config/routing_sections/README.md`). This module never invents a section:

- **Production** (`production_status`) is READY only when ALL of these hold:
  1. the site's file exists and declares exactly one `m3_to_m4_inflow` section that validates;
  2. that entry carries a complete named approval (`approved_by` + `approved_at`) -- tooling never
     fills these in; the file's presence alone approves nothing;
  3. its `extraction_method` is in `APPROVED_PRODUCTION_METHODS`;
  4. the existing production gate (`production_gate.evaluate(model="sph")`) reports the checklist item
     `m3_to_m4_routed_section` as READY (the team's own input-resolution / approval record).
  Today (2) is unfilled for every site, (3) is empty because decision S2 is DEFERRED, and (4) is
  BLOCKED for Teesta -- so production routing is BLOCKED, by design.
- **Controlled** operation (`controlled_section`) may use the declared section even if unapproved,
  or the Feature 6 derived section (normal to the M1 centreline at the near-field inflow point,
  `config/m4_sph.yaml` `routed_section_width_m`). Either way the result is labelled controlled and
  carries the method's upper-bound caveat; it is never a production section.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .routed_discharge import RoutedDischargeInvalid, check_section, crs_epsg

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "routing_sections"
PURPOSE = "m3_to_m4_inflow"

#: The existing controlled extraction (`section_discharge.py`): depth x speed magnitude x cut length.
CONTROLLED_METHOD_ID = "depth_x_speed_magnitude_line_integral"
CONTROLLED_METHOD_CAVEATS = (
    "speed_magnitude_as_section_normal_velocity: the D-Flow FM map stores speed magnitude only, so Q is an "
    "upper bound wherever flow crosses the section obliquely",
    "controlled_extraction_not_production: section and extraction method are not approved for production "
    "routing (Feature 17 decision S2 deferred)",
)
#: Decision S2 (Feature 17) is DEFERRED: no extraction method is approved for production routing.
#: Adding one here requires a recorded decision in docs/decisions.md.
APPROVED_PRODUCTION_METHODS: tuple[str, ...] = ()

READY, BLOCKED = "READY", "BLOCKED"


class RoutingSectionError(ValueError):
    """The routing-section file exists but is malformed."""


def config_path(site_id: str, config_dir: str | Path | None = None) -> Path:
    from .routed_discharge import check_scenario_id
    check_scenario_id(site_id, "site_id")
    return Path(config_dir or CONFIG_DIR) / f"{site_id}.yaml"


def load(site_id: str, *, config_dir: str | Path | None = None, site_epsg: int | None = None) -> dict | None:
    """The validated `m3_to_m4_inflow` entry for `site_id`, or None when no file exists."""
    path = config_path(site_id, config_dir)
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("site_id") != site_id:
        raise RoutingSectionError(f"{path.name}: site_id {raw.get('site_id')!r} does not match {site_id!r}")
    entries = [s for s in raw.get("sections") or [] if s.get("purpose") == PURPOSE]
    if len(entries) != 1:
        raise RoutingSectionError(f"{path.name}: expected exactly one '{PURPOSE}' section, found {len(entries)}")
    entry = dict(entries[0])
    for key in ("id", "geometry", "extraction_method", "source"):
        if not entry.get(key):
            raise RoutingSectionError(f"{path.name}: section is missing '{key}'")
    try:
        check_section(entry["geometry"], site_epsg)
    except RoutedDischargeInvalid as exc:
        raise RoutingSectionError(f"{path.name}: {exc}") from None
    approval = entry.get("approval") or {}
    filled = [bool(approval.get("approved_by")), bool(approval.get("approved_at"))]
    if any(filled) and not all(filled):
        raise RoutingSectionError(f"{path.name}: approval must name both approved_by and approved_at")
    entry["approved"] = all(filled)
    entry["path"] = str(path)
    return entry


def production_status(site_id: str, *, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
                      config_dir: str | Path | None = None, checklist_path=None) -> dict:
    """READY / BLOCKED (+ every reason) for production M3 -> M4 routing at `site_id`. Deterministic."""
    from . import production_gate
    kwargs = {} if checklist_path is None else {"checklist_path": checklist_path}
    gate = production_gate.evaluate(site_id, data_dir=data_dir, sites_dir=sites_dir, model="sph", **kwargs)
    reasons: list[str] = []
    entry = None
    site_epsg = None
    if gate.get("config_loaded"):
        from backend.m3_dflowfm.preflight import _load_config
        from backend.shared.site_config import default_data_dir
        try:
            cfg, _ = _load_config(site_id, Path(data_dir) if data_dir else default_data_dir(),
                                  Path(sites_dir) if sites_dir else None)
            site_epsg = int(cfg.crs.utm_epsg.value) if cfg.crs.utm_epsg.value is not None else None
        except Exception:  # noqa: BLE001 - gate already reports the config problem
            site_epsg = None
    try:
        entry = load(site_id, config_dir=config_dir, site_epsg=site_epsg)
    except (RoutingSectionError, RoutedDischargeInvalid) as exc:
        reasons.append(f"routing-section file invalid: {exc}")
    if entry is None and not reasons:
        reasons.append(f"no reviewed routing-section file config/routing_sections/{site_id}.yaml")
    if entry is not None:
        if not entry["approved"]:
            reasons.append(f"routing section '{entry['id']}' has no named approval (approved_by + approved_at)")
        if entry["extraction_method"] not in APPROVED_PRODUCTION_METHODS:
            reasons.append(f"extraction method '{entry['extraction_method']}' is not approved for production "
                           "(decision S2 deferred; no production method approved)")
    item = next((i for i in gate.get("items", []) if i["key"] == "m3_to_m4_routed_section"), None)
    if item is None:
        reasons.append("production checklist has no m3_to_m4_routed_section record for this site")
    elif item["status"] != READY:
        reasons.append(f"checklist m3_to_m4_routed_section is {item['status']}: {item.get('detail', '')}".strip())
    return {"site_id": site_id, "status": READY if not reasons else BLOCKED, "reasons": reasons,
            "section": entry, "approved_production_methods": list(APPROVED_PRODUCTION_METHODS),
            "gate_verdict": gate.get("verdict"), "gate_blocking": gate.get("blocking", [])}


def controlled_section(cfg, data_dir: str | Path, width_m: float, *, config_dir: str | Path | None = None):
    """`(LineString, section_meta)` for CONTROLLED (non-production) extraction. Declared-but-unapproved
    sections are used as declared; otherwise the Feature 6 derived section. Never production."""
    import geopandas as gpd
    from pyproj import Transformer
    from shapely.geometry import LineString

    from .section_discharge import normal_section

    site_id = cfg.site.id
    epsg = int(cfg.crs.utm_epsg.value)
    entry = load(site_id, config_dir=config_dir, site_epsg=epsg)
    if entry is not None:
        geom = entry["geometry"]
        return LineString([tuple(map(float, c[:2])) for c in geom["coordinates"]]), {
            "section_source": "config_routing_section", "section_id": entry["id"], "section_file": entry["path"],
            "section_approved": entry["approved"], "section_source_ref": entry["source"],
            "declared_extraction_method": entry["extraction_method"], "epsg": crs_epsg(geom["crs"])}
    location = cfg.domains.near_field.inflow.location.value
    if location is None:
        raise ValueError("domains.near_field.inflow.location is null")
    x, y = Transformer.from_crs(4326, epsg, always_xy=True).transform(*location)
    centreline = gpd.read_file(Path(data_dir) / site_id / "terrain" / "centreline.gpkg").to_crs(epsg=epsg)
    line = max(centreline.geometry, key=lambda g: g.length)
    return normal_section(line, x, y, width_m), {
        "section_source": "derived_normal_to_centreline", "section_id": None, "section_approved": False,
        "derivation": {"inflow_location_lonlat": list(location),
                       "inflow_location_status": cfg.domains.near_field.inflow.location.status,
                       "width_m": float(width_m), "width_setting": "config/m4_sph.yaml routed_section_width_m"},
        "epsg": epsg}

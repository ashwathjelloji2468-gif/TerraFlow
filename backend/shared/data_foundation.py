"""Feature 16 P2: read-only per-site Data Foundation view.

Answers "what site inputs do we have, where did they come from, what artifacts exist, what is
missing, and what still needs resolution?" by AGGREGATING evidence that existing systems already
produce. It is not a readiness system of its own and never changes their meanings:

- configured values + their status    -> `backend.m3_dflowfm.production_gate` (READY / PLACEHOLDER /
                                          BLOCKED per value; the gate verdict is quoted, not recomputed)
- input-resolution status             -> `backend.shared.input_resolution.report` / `.validate`
                                          (APPROVED / CANDIDATE / UNRESOLVED, plus REJECTED and dataset
                                          checks when the register tooling reports them) -- passed through
                                          verbatim
- raw M1 ingestion provenance         -> `data/<site>/raw/provenance.json`, `raw/dem_comparison.json`
                                          (entry `status`/`reason`/`kind` quoted verbatim)
- terrain artifacts                   -> `production_gate.TERRAIN_FILES` / `terrain_item` and
                                          `data/<site>/terrain/provenance.json`

The only check this module adds is a file checksum against the sha256 a provenance entry already
records for that same file: VERIFIED / MISMATCH / UNVERIFIABLE (file missing, or no checksum of that
file recorded). Read-only: nothing is written, approved, applied or filled in. Output is
deterministic (no timestamps; machine-independent path labels).

CLI: python -m backend.shared.data_foundation <site_id> [--data-dir DIR] [--format json|markdown]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from backend.shared import input_resolution as ir
from backend.shared.site_config import SITES_DIR, default_data_dir, onboarded_config_path

REPORT_VERSION = 1
# File-check vocabulary (the only statuses this module originates).
VERIFIED, MISMATCH, UNVERIFIABLE = "VERIFIED", "MISMATCH", "UNVERIFIABLE"
# Presence vocabulary for provenance / artifact files.
PRESENT, MISSING, UNREADABLE = "PRESENT", "MISSING", "UNREADABLE"
#: raw/provenance.json keys whose `sha256` is the checksum of the entry's own `file`
#: (download.py, discharge.py). HydroBASINS records the checksum of its SOURCE shapefile instead.
FILE_HASHED_KEYS_PREFIX = ("dem_",)
FILE_HASHED_KEYS = ("landcover_esa_worldcover", "discharge_glofas")

STATUS_LEGEND = {
    "values.status": "production_gate: READY (sourced) | PLACEHOLDER (value is a placeholder) | BLOCKED (value null)",
    "values.resolution": "input_resolution: APPROVED | CANDIDATE (awaiting a named reviewer) | REJECTED | "
                         "UNRESOLVED (no register entry); null = the gate does not list this value as blocking",
    "file_check": "VERIFIED (sha256 matches provenance) | MISMATCH | UNVERIFIABLE (file missing or no checksum of it recorded)",
    "provenance/artifact files": "PRESENT | MISSING | UNREADABLE",
    "product.status": "quoted verbatim from raw/provenance.json (e.g. fetched, unavailable)",
    "gate_verdict": "production_gate verdict, quoted (READY | BLOCKED)",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> tuple[str, dict | None]:
    if not path.is_file():
        return MISSING, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNREADABLE, None
    return (PRESENT, data) if isinstance(data, dict) else (UNREADABLE, None)


def _locate_config(site_id: str, data_dir: Path, sites_dir: Path | None) -> tuple[Path, str, str]:
    """(directory, origin, label) by the same precedence as `production_gate`/`preflight._load_config`."""
    if sites_dir is not None:
        return Path(sites_dir), "explicit", f"<sites_dir>/{site_id}.yaml"
    onboarded = onboarded_config_path(site_id, data_dir)
    if onboarded.is_file():
        return onboarded.parent, "onboarded", f"<data_dir>/{site_id}/config/{site_id}.yaml"
    return SITES_DIR, "bundled", f"sites/{site_id}.yaml"


def _file_check(key: str, entry: dict, raw_dir: Path) -> dict | None:
    if not entry.get("file"):
        return None  # nothing on disk is claimed
    path = raw_dir / entry["file"]
    hashed = key in FILE_HASHED_KEYS or key.startswith(FILE_HASHED_KEYS_PREFIX)
    if not path.is_file():
        return {"status": UNVERIFIABLE, "detail": f"raw/{entry['file']} is not present"}
    if not hashed or not entry.get("sha256"):
        return {"status": UNVERIFIABLE, "detail": "no checksum of this file is recorded in its provenance entry"}
    ok = _sha256(path) == entry["sha256"]
    return {"status": VERIFIED if ok else MISMATCH,
            "detail": "sha256 matches raw/provenance.json" if ok else "sha256 differs from raw/provenance.json"}


def _product(key: str, entry: dict, raw_dir: Path) -> dict:
    keep = ("status", "dataset", "source", "file", "reason", "kind", "license", "vertical_datum")
    return {"key": key, **{k: entry[k] for k in keep if k in entry}, "file_check": _file_check(key, entry, raw_dir)}


def build(site_id: str, *, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
          register_dir: Path = ir.REGISTER_DIR, checklist_path: str | Path | bool | None = None,
          model: str = "delft3d") -> dict:
    """The Data Foundation view for `site_id` (module docstring). Read-only and deterministic."""
    from backend.m3_dflowfm import production_gate as pg

    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    # None -> the team checklist (docs/real_input_checklist.json, as production_gate); False -> none.
    checklist_path = pg.CHECKLIST_PATH if checklist_path is None else (None if checklist_path is False else checklist_path)
    cfg_dir, origin, label = _locate_config(site_id, data_dir, Path(sites_dir) if sites_dir else None)
    site_dir = data_dir / site_id
    warnings: list[str] = []
    report: dict = {"report": "data_foundation", "report_version": REPORT_VERSION, "site_id": site_id,
                    "read_only": True, "status_legend": STATUS_LEGEND}

    # A. identity / config ---------------------------------------------------------------------
    gate = pg.evaluate(site_id, data_dir=data_dir, sites_dir=cfg_dir, checklist_path=checklist_path, model=model)
    gate.pop("evaluated_at", None)  # determinism
    ident = {"config_loaded": gate["config_loaded"], "config_origin": origin, "config_path": label}
    cfg = None
    if gate["config_loaded"]:
        from backend.m3_dflowfm.preflight import _load_config
        cfg, _ = _load_config(site_id, data_dir, cfg_dir)
        ident.update(name=cfg.site.name, region=cfg.site.region, river=cfg.site.river,
                     crs_epsg=cfg.crs.utm_epsg.value, dams=[d.id for d in cfg.dams],
                     points_of_interest=len(cfg.points_of_interest), events=[e.id for e in cfg.events])
    else:
        ident["error"] = gate["items"][0]["detail"] if gate["items"] else "site config could not be loaded"
    report["A_site"] = ident

    # F (computed first: B joins it). Input-resolution register, reused verbatim ---------------
    register_file = Path(register_dir) / f"{site_id}.yaml"
    res: dict = {"register_path": f"config/input_resolution/{site_id}.yaml" if Path(register_dir) == ir.REGISTER_DIR
                 else f"<register_dir>/{site_id}.yaml",
                 "register_present": register_file.is_file()}
    rows_by_key: dict = {}
    if cfg is not None:
        ir_report = ir.report(site_id, data_dir=data_dir, sites_dir=cfg_dir, register_dir=Path(register_dir),
                              checklist_path=checklist_path)
        ir_report.pop("evaluated_at", None)
        rows_by_key = {r["key"]: r for r in ir_report["rows"]}
        problems = ir.validate(site_id, sites_dir=cfg_dir, register_dir=Path(register_dir)) if res["register_present"] else []
        reg = ir.load_register(site_id, Path(register_dir))
        res.update(counts=ir_report["counts"], rows=ir_report["rows"],
                   evidence_only=len(reg.get("evidence_only") or []), validation_problems=problems,
                   valid=not problems)
        warnings += [f"input-resolution register: {p}" for p in problems]
        if not res["register_present"]:
            warnings.append("no input-resolution register for this site: every blocking value is UNRESOLVED")

    # B. configured values (production_gate's per-value status, joined with the register) -----
    values = []
    if cfg is not None:
        svs = dict(pg._walk(cfg))
        for item in gate["items"]:
            if item["category"] != "site_config" or item["key"] not in svs:
                continue
            sv, row = svs[item["key"]], rows_by_key.get(item["key"])
            values.append({"key": item["key"], "status": item["status"], "config_status": sv.status,
                           "unit": sv.unit, "source": sv.source, "blocking": item["blocking"],
                           "resolution": row["resolution"] if row else None,
                           **({"dataset_check": row["dataset_check"]} if row and "dataset_check" in row else {})})
    report["B_values"] = {"counts": {s: sum(1 for v in values if v["status"] == s) for s in (pg.READY, pg.PLACEHOLDER, pg.BLOCKED)},
                          "items": values}

    # C. raw terrain / M1 ingestion provenance -------------------------------------------------
    raw_dir = site_dir / "raw"
    prov_state, prov = _read_json(raw_dir / "provenance.json")
    sel_state, sel = _read_json(raw_dir / "dem_comparison.json")
    products = [_product(k, prov[k], raw_dir) for k in sorted(prov or {}) if isinstance(prov[k], dict)]
    terrain_products = [p for p in products if p["key"].startswith("dem_") or p["key"] == "landcover_esa_worldcover"]
    report["C_terrain_provenance"] = {
        "raw_provenance": prov_state, "raw_provenance_path": "<data_dir>/" + f"{site_id}/raw/provenance.json",
        "products": terrain_products,
        "dem_selection": {"file": sel_state, "selected": (sel or {}).get("selected"),
                          "threshold_met": (sel or {}).get("threshold_met"), "rule": (sel or {}).get("rule")},
    }
    if prov_state != PRESENT:
        warnings.append(f"raw ingestion provenance (raw/provenance.json) is {prov_state}: DEM/landcover origin unknown")
    if sel_state != PRESENT:
        warnings.append(f"DEM comparison (raw/dem_comparison.json) is {sel_state}: DEM selection not evidenced")
    elif sel.get("threshold_met") is False:
        warnings.append("DEM selection did not meet the void-fraction threshold (threshold_met: false)")

    # D. terrain artifacts ---------------------------------------------------------------------
    terrain_dir = site_dir / "terrain"
    tprov_state, tprov = _read_json(terrain_dir / "provenance.json")
    gate_terrain = next((i for i in gate["items"] if i["key"] == "terrain_artifacts"), None)
    report["D_terrain_artifacts"] = {
        "gate_item": gate_terrain,
        "files": [{"name": n, "state": PRESENT if (terrain_dir / n).is_file() else MISSING} for n in pg.TERRAIN_FILES],
        "terrain_provenance": tprov_state,
        "dem_product": ((tprov or {}).get("dem") or {}).get("product"),
        "has_placeholders": (tprov or {}).get("has_placeholders"),
        "placeholder_fields": len((tprov or {}).get("placeholder_fields") or []) if tprov else None,
    }
    if tprov_state != PRESENT:
        warnings.append(f"terrain provenance (terrain/provenance.json) is {tprov_state}")
    else:
        if tprov.get("has_placeholders"):
            warnings.append("terrain products were built from placeholder inputs (terrain/provenance.json has_placeholders: true)")
        if sel and tprov.get("dem", {}).get("product") not in (None, sel.get("selected")):
            warnings.append(f"terrain built from DEM '{tprov['dem']['product']}' but raw/dem_comparison.json selected "
                            f"'{sel.get('selected')}'")

    # E. hydrology inputs already known --------------------------------------------------------
    from backend.m1_terrain import discharge as dis
    hydro = {p["key"]: p for p in products if p["key"] in ("discharge_glofas", "hydrobasins_catchment")}
    if "discharge_glofas" in hydro and (prov or {}).get("discharge_glofas", {}).get("dataset") == dis.DATASET:
        hydro["discharge_glofas"]["modelled_not_gauged"] = True  # discharge.py: GloFAS is modelled, never gauged
        if "summary_m3s" in prov["discharge_glofas"]:
            hydro["discharge_glofas"]["summary_m3s"] = prov["discharge_glofas"]["summary_m3s"]
    base_flow = next((v for v in values if v["key"] == "domains.far_field.inflow.base_flow"), None)
    report["E_hydrology"] = {
        "discharge_glofas": hydro.get("discharge_glofas", {"status": None, "detail": "no provenance entry"}),
        "hydrobasins_catchment": hydro.get("hydrobasins_catchment", {"status": None, "detail": "no provenance entry"}),
        "config_base_flow": base_flow,
        "note": "GloFAS discharge is modelled (reanalysis/forecast), not gauged; it is context only and is never "
                "written into domains.far_field.inflow.base_flow (backend/m1_terrain/discharge.py).",
    }
    report["F_input_resolution"] = res

    # file-check warnings (every product) ------------------------------------------------------
    for p in products:
        fc = p.get("file_check")
        if fc and fc["status"] != VERIFIED:
            warnings.append(f"raw/{p.get('file')} ({p['key']}): {fc['status']} -- {fc['detail']}")

    # G. blocking / unresolved items (gate's blocking list, quoted; register state joined) -----
    blocking = []
    for item in gate["items"]:
        if not item["blocking"] or item["status"] == pg.READY:
            continue
        row = rows_by_key.get(item["key"])
        blocking.append({"key": item["key"], "category": item["category"], "status": item["status"],
                         "resolution": row["resolution"] if row else None, "detail": item["detail"]})
    report["G_blocking"] = {"gate_verdict": gate["verdict"], "model": model, "count": len(blocking), "items": blocking}

    # I. optional POI evidence (Feature 16 P4; exposure/poi_evidence.json, quoted) -------------
    ev_state, ev = _read_json(site_dir / "exposure" / "poi_evidence.json")
    poi = {"file": ev_state, "path": f"<data_dir>/{site_id}/exposure/poi_evidence.json"}
    if ev:
        poi.update(authority=ev.get("authority"), counts=ev.get("counts"),
                   candidates=[{k: c.get(k) for k in ("target", "status", "review_state", "proposed_value")}
                               for c in ev.get("candidates") or []],
                   unresolved=[{k: u.get(k) for k in ("target", "status", "reason")} for u in ev.get("unresolved") or []],
                   dataset_checks={})
        for key, d in sorted((ev.get("datasets") or {}).items()):
            p = site_dir / str(d.get("path") or "")
            if not d.get("path") or not p.resolve().is_relative_to(site_dir.resolve()):
                poi["dataset_checks"][key] = UNVERIFIABLE
                warnings.append(f"POI evidence dataset path for {key!r} is outside the site folder: not read")
                continue
            ok = p.is_file() and d.get("sha256") and _sha256(p) == d["sha256"]
            status = VERIFIED if ok else (MISMATCH if p.is_file() and d.get("sha256") else UNVERIFIABLE)
            poi["dataset_checks"][key] = status
            if status != VERIFIED:
                warnings.append(f"POI evidence dataset {d.get('path')}: {status}")
    report["I_poi_evidence"] = poi

    # J. base-flow: configured value vs modelled GloFAS candidate (Feature 16 P5, quoted) ------
    bf_state, bf = _read_json(site_dir / "raw" / "base_flow_evidence.json")
    cand = {"file": bf_state, "path": f"<data_dir>/{site_id}/raw/base_flow_evidence.json"}
    if bf:
        ev_file = (bf.get("evidence") or {}).get("file") or ""
        p = site_dir / str(ev_file)
        if not ev_file or not p.resolve().is_relative_to(site_dir.resolve()):
            check = UNVERIFIABLE
        elif not p.is_file():
            check = UNVERIFIABLE
        else:
            check = VERIFIED if _sha256(p) == (bf.get("evidence") or {}).get("sha256") else MISMATCH
        cand.update(status=bf.get("status"), review_state=bf.get("review_state"), proposed_value=bf.get("proposed_value"),
                    unit=bf.get("unit"), modelled_not_gauged=bf.get("modelled_not_gauged"),
                    statistics_m3s=bf.get("statistics_m3s"), dataset_check=check)
        if check != VERIFIED:
            warnings.append(f"base-flow GloFAS evidence {ev_file or '<no file>'}: {check}")
    report["J_base_flow"] = {
        "configured": base_flow,
        "modelled_glofas_candidate": cand,
        "note": "The configured value (B_values) is what the model uses. The GloFAS record is modelled evidence "
                "awaiting review; it is never applied and never replaces the configured value.",
    }

    # H. provenance warnings -------------------------------------------------------------------
    report["H_warnings"] = warnings
    return report


def to_markdown(rep: dict) -> str:
    a, b, g = rep["A_site"], rep["B_values"], rep["G_blocking"]
    lines = [f"# Data Foundation: `{rep['site_id']}`", "",
             "Read-only aggregation of existing evidence (production gate, input-resolution register, M1 "
             "provenance). It approves, applies and fills in nothing.", "",
             f"## A. Site\n\nconfig: `{a['config_path']}` ({a['config_origin']}), loaded: {a['config_loaded']}"]
    if a.get("name"):
        lines.append(f"name: {a['name']} · dams: {', '.join(a['dams'])} · POIs: {a['points_of_interest']}")
    lines += ["", "## B. Configured values", "", " · ".join(f"{k} {v}" for k, v in b["counts"].items()), "",
              "| value | status | resolution | unit |", "|---|---|---|---|"]
    lines += [f"| `{v['key']}` | {v['status']} | {v['resolution'] or '—'} | {v['unit']} |" for v in b["items"]]
    c = rep["C_terrain_provenance"]
    lines += ["", "## C. Terrain/DEM provenance", "", f"raw/provenance.json: {c['raw_provenance']} · "
              f"dem_comparison.json: {c['dem_selection']['file']} · selected: {c['dem_selection']['selected']}", ""]
    lines += [f"- `{p['key']}`: {p.get('status')}" + (f" · file {p['file_check']['status']}" if p.get("file_check") else "")
              + (f" · {p['reason']}" if p.get("reason") else "") for p in c["products"]]
    d = rep["D_terrain_artifacts"]
    lines += ["", "## D. Terrain artifacts", "", f"terrain/provenance.json: {d['terrain_provenance']}", ""]
    lines += [f"- `{f['name']}`: {f['state']}" for f in d["files"]]
    e = rep["E_hydrology"]
    lines += ["", "## E. Hydrology", "", f"- discharge_glofas: {e['discharge_glofas'].get('status')}",
              f"- hydrobasins_catchment: {e['hydrobasins_catchment'].get('status')}", f"- {e['note']}"]
    f = rep["F_input_resolution"]
    lines += ["", "## F. Input-resolution register", "", f"`{f['register_path']}` present: {f['register_present']}"
              + (f" · valid: {f['valid']} · " + " · ".join(f"{k} {v}" for k, v in f["counts"].items()) if "counts" in f else "")]
    lines += ["", f"## G. Blocking ({g['count']}; gate verdict {g['gate_verdict']})", ""]
    lines += [f"- `{i['key']}` ({i['category']}): {i['status']}" + (f" · {i['resolution']}" if i["resolution"] else "")
              for i in g["items"]]
    lines += ["", "## H. Provenance warnings", ""] + [f"- {w}" for w in rep["H_warnings"]] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only per-site Data Foundation report")
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    rep = build(args.site_id, data_dir=args.data_dir)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str) if args.format == "json" else to_markdown(rep))
    return 0 if rep["A_site"]["config_loaded"] else 1


if __name__ == "__main__":
    sys.exit(main())

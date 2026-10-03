"""Thin M0 orchestration for the contract-backed onboarding preparation steps.

This module passes the canonical ``SiteConfig`` and M2 scenario design between
modules. M1 and M2 remain independent of the API and each other.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from backend.m1_terrain.pipeline import build_terrain
from backend.m5_emulator import scenario_design
from backend.m5_emulator.scenario_design import ScenarioDesignSettings
from backend.shared.site_config import load_site_config


def materialize_site_config(site_id: str, config: dict, data_dir: str | Path) -> Path:
    """Persist the accepted JSON config in YAML form for module loaders."""
    root = Path(data_dir) / site_id / "config"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{site_id}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _load_config(site_id: str, data_dir: str | Path, config: dict | None = None):
    root = Path(data_dir) / site_id / "config"
    path = root / f"{site_id}.yaml"
    if config is not None:
        materialize_site_config(site_id, config, data_dir)
    if not path.is_file():
        raise FileNotFoundError(f"onboarding site config is missing: {path}")
    return load_site_config(site_id, sites_dir=root)


def prepare_terrain(site_id: str, data_dir: str | Path, config: dict | None = None,
                    event=None) -> dict:
    """Feature 2: ingest the site's raw data (DEM candidates, WorldCover, HydroBASINS, discharge)
    into `raw/`, select the DEM deterministically (`raw/dem_comparison.json`), then run M1.

    Raises `backend.m1_terrain.ingest.IngestionError` (carrying the job error code, e.g.
    `terrain_inputs_incomplete`) on a blocking ingestion failure."""
    from backend.m1_terrain import ingest

    cfg = _load_config(site_id, data_dir, config)
    site_dir = Path(data_dir) / site_id
    raw_dir = site_dir / "raw"
    summary = ingest.ingest_site(cfg, raw_dir, event=event)
    # M7's latest observed lake outline, when Earth Engine has produced one, overrides the
    # WorldCover-derived lake extent (water.py's documented hook).
    lake = site_dir / "gee" / "lake_latest.geojson"
    water_polygon = None
    if lake.is_file():
        try:
            if json.loads(lake.read_text(encoding="utf-8")).get("features"):
                water_polygon = lake
        except ValueError:
            water_polygon = None
    result = build_terrain(cfg, summary["selected_dem"], raw_dir, site_dir / "terrain",
                           water_polygon_path=water_polygon)
    prepare_poi_evidence(cfg, data_dir, event=event)
    return result


#: Feature 16 P4: optional onboarding step settings (OFF by default).
ONBOARDING_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "onboarding.yaml"


def poi_evidence_enabled(path: str | Path | None = None) -> bool:
    path = Path(path or ONBOARDING_SETTINGS_PATH)
    if not path.is_file():
        return False
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    value = (raw.get("poi_evidence") or {}).get("enabled", False)
    return value is True  # only an explicit boolean true enables it


def prepare_poi_evidence(cfg, data_dir: str | Path, event=None, *, enabled: bool | None = None) -> dict | None:
    """Optional (config/onboarding.yaml `poi_evidence.enabled`, default false): match the site's POIs
    against an OSM extract ALREADY present in `<data>/<site>/exposure/` (no download, no network).
    Writes `exposure/poi_evidence.json` with pending CANDIDATE / UNRESOLVED records only. Never
    raises and never fails onboarding: a missing extract is reported UNVERIFIABLE and skipped."""
    event = event or (lambda msg: None)
    if not (poi_evidence_enabled() if enabled is None else enabled):
        return None
    from backend.m6_impact import poi_candidates
    try:
        out = poi_candidates.run(cfg, data_dir=data_dir)
    except Exception as e:  # noqa: BLE001 -- optional step: report, never fail the job
        event(f"poi evidence: skipped ({type(e).__name__}: {e})")
        return {"site_id": cfg.site.id, "status": "error", "reason": type(e).__name__}
    if out["status"] == poi_candidates.UNVERIFIABLE:
        event(f"poi evidence: {out['reason']} (optional; onboarding continues)")
    else:
        event(f"poi evidence: {out['counts']} written={out['written']} (pending review; nothing approved)")
    out.pop("result", None)
    return out


def prepare_breach(site_id: str, data_dir: str | Path, event=None) -> Path:
    """Feature 3: run M2 for every dam (breach parameters, reference low/high hydrographs,
    Feature 2 evidence) and write `breach/breach_params.json` + `breach/hydrographs/`."""
    from backend.m2_breach.reference import run_breach_stage

    cfg = _load_config(site_id, data_dir)
    return run_breach_stage(cfg, data_dir=Path(data_dir), event=event)


def prepare_design(site_id: str, data_dir: str | Path, *, demo: bool = False, event=None) -> Path:
    """Run M5's scenario design over M2's computed ranges."""
    cfg = _load_config(site_id, data_dir)
    target_dam = cfg.domains.far_field.inflow.from_
    settings = scenario_design.load_scenario_design_settings()
    if demo:
        settings = ScenarioDesignSettings(
            n=4, n_holdout=0, seed=settings.seed,
            input_widen_fraction=settings.input_widen_fraction, method=settings.method,
        )
    path = scenario_design.write_scenario_design(
        cfg, target_dam, data_dir=Path(data_dir), settings=settings,
    )
    if event is not None:
        import json as _json

        design = _json.loads(path.read_text(encoding="utf-8"))
        prov = design.get("provenance", {})
        event(f"design {target_dam}: {len(design['scenarios'])} scenarios + {len(design['extra'])} extra, "
              f"sampled {', '.join(prov.get('sampled_inputs', []))}; hydrograph {prov.get('hydrograph_method')}; "
              f"{len(prov.get('rejected', []))} rejected; fingerprint {str(prov.get('fingerprint'))[:12]}")
    return path

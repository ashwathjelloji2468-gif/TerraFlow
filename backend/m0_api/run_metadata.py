"""Feature 12: where a registered run's auxiliary artifacts live, from run metadata / config
instead of hardcoded site or run IDs (`config/registered_runs.yaml`, `config/literature_references.yaml`).

Resolution order for each key: the run's own `run_meta.json` -> `config/registered_runs.yaml`
-> generic default. All paths resolve under the data directory, never the process cwd."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_CONFIG = REPO_ROOT / "config" / "registered_runs.yaml"
LITERATURE_CONFIG = REPO_ROOT / "config" / "literature_references.yaml"


@lru_cache(maxsize=4)
def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def run_config(run_id: str | None) -> dict:
    return dict((_load(RUNS_CONFIG).get("registered_runs") or {}).get(run_id or "", {}) or {})


def site_config(site_id: str) -> dict:
    return dict((_load(RUNS_CONFIG).get("sites") or {}).get(site_id, {}) or {})


def _merged(run_id: str | None, run_meta: dict | None) -> dict:
    cfg = run_config(run_id)
    for key in ("terrain_site_id", "forcing_files", "dam_id", "input_forcing_note"):
        if run_meta and run_meta.get(key) not in (None, "", []):
            cfg[key] = run_meta[key]
    return cfg


def terrain_dir(site_dir: Path, run_id: str | None, run_meta: dict | None = None) -> Path:
    """Terrain folder whose DEM/POIs match this run's mesh (default: the site's own)."""
    other = _merged(run_id, run_meta).get("terrain_site_id")
    return (site_dir.parent / other / "terrain") if other else (site_dir / "terrain")


def forcing_candidates(site_dir: Path, case_dir: Path, run_id: str | None, run_meta: dict | None = None) -> list[Path]:
    scenario_id = (run_meta or {}).get("scenario_id") or (run_id.rsplit("__", 1)[0] if run_id else None)
    out = [case_dir / "inputs" / f"{scenario_id}_forcing.csv"] if scenario_id else []
    out += [site_dir.parent / rel for rel in (_merged(run_id, run_meta).get("forcing_files") or [])]
    return out


def dam_id(site_id: str, run_id: str | None, run_meta: dict | None = None) -> str:
    explicit = _merged(run_id, run_meta).get("dam_id")
    if explicit:
        return str(explicit)
    try:
        from backend.shared.site_config import load_site_config
        dams = load_site_config(site_id).dams
        if dams:
            return dams[0].id
    except Exception:
        pass
    return "unknown_dam"


def input_forcing_note(run_id: str | None, run_meta: dict | None = None) -> str | None:
    return _merged(run_id, run_meta).get("input_forcing_note")


def default_compare_scenario(site_id: str) -> str | None:
    return site_config(site_id).get("default_compare_scenario")


def literature(site_id: str) -> dict:
    """`{poi, citations}` for this site, or `{}` when none is configured."""
    return dict((_load(LITERATURE_CONFIG).get("sites") or {}).get(site_id, {}) or {})

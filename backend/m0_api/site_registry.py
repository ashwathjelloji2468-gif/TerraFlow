"""The site registry: which sites TerraFlow serves, where their configs live, and their state.

A site is *registered* when it is either

- **bundled**: listed in `config/sites.yaml` (`bundled:`) and present as `sites/<id>.yaml` in the
  repo (Teesta today), or
- **onboarded**: persisted by `POST /sites` at `<data dir>/<id>/config/<id>.yaml` (contract §1.8).

Nothing here hard-codes a site id. Configs are validated by the canonical `SiteConfig` model
(`backend/shared/site_config.py`); the SQLite registry keeps only the four contract §4.5 tables, so
site state is derived from the saved config plus the site's onboarding jobs.

Two named test-fixture lists, neither of them a site allow-list (neither grants any site access
that other sites lack):

- `TEST_FIXTURE_SITE_IDS`: synthetic configs shipped in `sites/` that the test suite onboards
  through `POST /sites` (`demo_valley`, `synth_engdam`). Every other `sites/*.yaml` id is reserved.
- `SYNTHETIC_FIXTURE_SITE_IDS`: of those, the I-1 fixture (`demo_valley`,
  `backend/m0_api/synthetic_demo.py`), which writes fixed synthetic artifacts instead of running
  M1/M2/M5 when onboarded with `demo_mode: true`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml
from pydantic import ValidationError

from backend.m0_api import jobs, registry
from backend.shared.site_config import (
    SITES_DIR, PlaceholderWarning, SiteConfig, SiteConfigError, load_site_config,
)

log = logging.getLogger("m0.site_registry")

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SITES_FILE = REPO_ROOT / "config" / "sites.yaml"

#: Repo `sites/` test fixtures that tests onboard through `POST /sites` (see module docstring).
TEST_FIXTURE_SITE_IDS = ("demo_valley", "synth_engdam")
#: The I-1 synthetic fixture routed to `synthetic_demo` (see module docstring).
SYNTHETIC_FIXTURE_SITE_IDS = ("demo_valley",)


class SiteRegistrationError(ValueError):
    """A site cannot be registered. `code` is the contract §2.7 error code."""

    def __init__(self, code: str, message: str, details: dict | None = None, status_code: int = 409):
        super().__init__(message)
        self.code, self.message, self.details, self.status_code = code, message, details or {}, status_code


# --- where configs live ------------------------------------------------------------------------
def bundled_site_ids() -> list[str]:
    """Bundled site ids from `config/sites.yaml` that actually have a `sites/<id>.yaml`."""
    if not BUNDLED_SITES_FILE.is_file():
        return []
    raw = yaml.safe_load(BUNDLED_SITES_FILE.read_text(encoding="utf-8")) or {}
    ids = raw.get("bundled") or []
    return [s for s in ids if isinstance(s, str) and (SITES_DIR / f"{s}.yaml").is_file()]


def onboarded_config_path(site_id: str) -> Path:
    return registry.data_dir() / site_id / "config" / f"{site_id}.yaml"


def onboarded_site_ids() -> list[str]:
    root = registry.data_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / "config" / f"{p.name}.yaml").is_file())


def list_site_ids() -> list[str]:
    """Every registered site: bundled first (config order), then onboarded, sorted."""
    bundled = bundled_site_ids()
    return bundled + [s for s in onboarded_site_ids() if s not in bundled]


def exists(site_id: str) -> bool:
    return site_id in bundled_site_ids() or onboarded_config_path(site_id).is_file()


def is_synthetic_fixture(site_id: str) -> bool:
    return site_id in SYNTHETIC_FIXTURE_SITE_IDS


def config_dir(site_id: str) -> Path:
    """The folder `load_site_config(site_id, sites_dir=...)` should read for a registered site."""
    return SITES_DIR if site_id in bundled_site_ids() else onboarded_config_path(site_id).parent


def load(site_id: str) -> SiteConfig:
    """Load a registered site's config. FileNotFoundError if not registered; SiteConfigError if
    the saved file no longer validates."""
    if not exists(site_id):
        raise FileNotFoundError(f"No site '{site_id}' is configured.")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)  # placeholders are reported via flags
        return load_site_config(site_id, sites_dir=config_dir(site_id))


# --- validation and registration ---------------------------------------------------------------
def _pydantic_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"field": ".".join(str(p) for p in err["loc"]) or "<root>", "message": err["msg"]}
            for err in exc.errors()]


def validate_config(site_config: dict) -> SiteConfig:
    """Full `SiteConfig` validation (field types + cross-checks) plus the registration rules a
    SiteSummary needs. Raises SiteRegistrationError(422) listing every invalid field."""
    try:
        cfg = SiteConfig.model_validate(site_config)
    except ValidationError as exc:
        errors = _pydantic_errors(exc)
        raise SiteRegistrationError(
            "invalid_site_config",
            f"site_config has {len(errors)} invalid field(s): "
            + "; ".join(f"{e['field']}: {e['message']}" for e in errors),
            {"errors": errors}, status_code=422) from exc
    problems = []
    if not cfg.site.name.strip():
        problems.append({"field": "site.name", "message": "a site name is required"})
    if cfg.domains.far_field.bbox.value is None:
        problems.append({"field": "domains.far_field.bbox.value",
                         "message": "the far-field study-area bounds are required to register a "
                                    "site (a placeholder value is allowed, a null one is not)"})
    if problems:
        raise SiteRegistrationError(
            "invalid_site_config",
            "; ".join(f"{p['field']}: {p['message']}" for p in problems),
            {"errors": problems}, status_code=422)
    return cfg


def _normalise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _taken_ids() -> set[str]:
    """Ids a new site may not use: every registered site, and every config file shipped in the
    repo's `sites/` (bundled or not -- e.g. `template`), except the test fixtures whose onboarding
    through the API is itself what the tests exercise."""
    repo = {p.stem for p in SITES_DIR.glob("*.yaml")} - set(TEST_FIXTURE_SITE_IDS)
    return repo | set(list_site_ids())


def _registered_names() -> dict[str, str]:
    names = {}
    for site_id in list_site_ids():
        try:
            names[_normalise_name(load(site_id).site.name)] = site_id
        except (FileNotFoundError, SiteConfigError) as exc:
            log.warning("site '%s' has an unreadable config, skipped for name check: %s", site_id, exc)
    return names


@contextmanager
def _registration_lock() -> Iterator[None]:
    path = registry.data_dir() / "sites.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def register(site_config: dict) -> Iterator[Path]:
    """Validate and persist a new site's config at `<data dir>/<id>/config/<id>.yaml`.

    A context manager so the caller can queue the onboarding job inside it: the config is on disk
    *before* the job exists, and it is removed again if queuing the job raises (no orphan site).
    Raises SiteRegistrationError: 422 invalid config, 409 `site_already_exists` / `site_name_taken`.
    """
    cfg = validate_config(site_config)
    site_id = cfg.site.id
    with _registration_lock():
        if site_id in _taken_ids():
            raise SiteRegistrationError("site_already_exists", f"A site with id '{site_id}' already exists.",
                                        {"site_id": site_id})
        clash = _registered_names().get(_normalise_name(cfg.site.name))
        if clash is not None:
            raise SiteRegistrationError(
                "site_name_taken", f"Site name '{cfg.site.name}' is already used by site '{clash}'.",
                {"site_id": site_id, "existing_site_id": clash, "name": cfg.site.name})
        path = onboarded_config_path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:  # exclusive create: never overwrite
            f.write(yaml.safe_dump(site_config, sort_keys=False))
        try:
            yield path
        except BaseException:
            path.unlink(missing_ok=True)
            raise


# --- derived state ---------------------------------------------------------------------------
def latest_onboarding_job(conn: sqlite3.Connection, site_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE site_id = ? AND kind = 'onboarding' ORDER BY created_at DESC, job_id DESC LIMIT 1",
        (site_id,),
    ).fetchone()


def _status(site_id: str) -> tuple[str, str | None]:
    """(status, status_reason_key) from the site's latest onboarding job (contract §5.1 enum)."""
    data_dir = registry.data_dir()
    if is_synthetic_fixture(site_id) and (data_dir / site_id / "demo_ready.json").is_file():
        return "demo_mode", "synthetic_demo"
    conn = registry.connect()
    try:
        row = latest_onboarding_job(conn, site_id)
    finally:
        conn.close()
    if row is None:
        return "onboarding", "configured_not_onboarded"
    stage = row["stage"]
    if stage == "failed":
        try:
            code = json.loads(row["error"] or "{}").get("error", {}).get("code")
        except ValueError:
            code = None
        return "failed", code or "onboarding_failed"
    if stage == "ready":
        return "ready", None
    return "onboarding", ("synthetic_demo" if is_synthetic_fixture(site_id) else f"job_stage_{stage}")


def _trained_models(site_id: str) -> tuple[list[str], str | None]:
    base = registry.data_dir() / site_id / "emulator"
    models, trained_at = [], None
    for model in ("delft3d", "sph"):
        manifest = base / model / "manifest.json"
        if manifest.is_file():
            models.append(model)
            try:
                stamp = json.loads(manifest.read_text()).get("trained_at")
            except ValueError:
                stamp = None
            if stamp and (trained_at is None or stamp > trained_at):
                trained_at = stamp
    return models, trained_at


def summary(site_id: str, cfg: SiteConfig | None = None) -> dict[str, Any]:
    """Contract §5.1 SiteSummary built from the saved config and real job/emulator state (before
    `site_status.overlay` adds the re-check schedule)."""
    cfg = cfg or load(site_id)
    status, reason = _status(site_id)
    models, trained_at = _trained_models(site_id)
    return {
        "site_id": site_id,
        "name": cfg.site.name,
        "status": status,
        "status_reason_key": reason,
        "emulator_ready": bool(models),
        "models_available": models,
        "last_trained_at": trained_at,
        "bbox_lonlat": [float(v) for v in cfg.domains.far_field.bbox.value],
        "events": [e.id for e in cfg.events],
        "has_placeholders": bool(cfg.has_placeholders),
    }


def _sourced(sv) -> dict[str, Any]:
    out = {"value": sv.value, "unit": sv.unit, "source": sv.source, "status": sv.status}
    if sv.note:
        out["note"] = sv.note
    return out


def _bbox_feature(bbox, domain: str, synthetic: bool) -> dict:
    west, south, east, north = bbox.value
    properties = {"domain": domain, "status": bbox.status, "source": bbox.source}
    if synthetic:
        properties["synthetic"] = True
    return {"type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north],
                                                             [west, north], [west, south]]]},
            "properties": properties}


def _centreline(site_id: str) -> dict:
    """M1's centreline, when M1 has written it; otherwise an empty collection (never invented)."""
    path = registry.data_dir() / site_id / "terrain" / "centreline.gpkg"
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}
    import geopandas as gpd

    frame = gpd.read_file(path).to_crs(4326)
    return json.loads(frame.to_json())


def detail(site_id: str) -> dict[str, Any]:
    """Contract §5.1 SiteDetail built entirely from the saved config (+ M1 centreline if present)."""
    cfg = load(site_id)
    out = summary(site_id, cfg)
    synthetic = is_synthetic_fixture(site_id)
    domain = [_bbox_feature(cfg.domains.far_field.bbox, "far_field", synthetic)]
    if cfg.domains.near_field.bbox.value is not None:
        domain.append(_bbox_feature(cfg.domains.near_field.bbox, "near_field", synthetic))
    dams = []
    for i, dam in enumerate(cfg.dams):
        specs = {"location": _sourced(dam.location), "breach_location": _sourced(dam.breach_location)}
        for name in type(dam.breach_inputs).model_fields:
            specs[name] = _sourced(getattr(dam.breach_inputs, name))
        dams.append({"dam_id": f"{site_id}__{dam.id}", "name": dam.name, "kind": dam.kind,
                     "order": i + 1, "key_specs": specs})
    pois = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": poi.location.value},
             "properties": {"poi_id": f"{site_id}__poi__{poi.id}", "name": poi.name, "kind": poi.category,
                            "status": poi.location.status}}
            for poi in cfg.points_of_interest if poi.location.value is not None]
    caveats = []
    if is_synthetic_fixture(site_id):
        caveats.append({"id": "synthetic_demo", "severity": "warning", "text_key": "caveat_synthetic_demo"})
    if cfg.has_placeholders:
        caveats.append({"id": "placeholder_inputs", "severity": "warning", "text_key": "caveat_placeholder_inputs"})
    out.update({
        "dams": dams,
        "emulator_inputs": [],
        "domain": {"type": "FeatureCollection", "features": domain},
        "centreline": _centreline(site_id),
        "pois": {"type": "FeatureCollection", "features": pois},
        "validation_summary": {"extent": "UNKNOWN", "depth": "UNKNOWN", "arrival": "UNKNOWN", "velocity": "UNKNOWN"},
        "caveats": caveats,
    })
    return out

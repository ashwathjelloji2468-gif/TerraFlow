"""Feature 2: dynamic data ingestion for any registered site (DEMs, land cover, HydroBASINS,
river discharge) -> `data/<site_id>/raw/`, plus the DEM comparison and deterministic selection.

Runs inside the onboarding job's existing `terrain` stage (`backend/m0_api/onboarding.py`), before
`pipeline.build_terrain` -- the contract §5.3 stage list is unchanged. Settings come from
`config/m1_ingestion.yaml` (docs/decisions.md 2026-10-01, D1-D6).

Order: readiness check (every config field M1 needs, listed at once) -> DEM candidates
(cached files validated against the requested area, else downloaded) -> ESA WorldCover (required;
network failures and coverage gaps are errors) -> DEM comparison + selection
(`raw/dem_comparison.json`) -> HydroBASINS catchment (optional, D3/D5) -> GloFAS discharge
(optional, D6). Every product gets a `raw/provenance.json` entry; a product that was not fetched is
recorded `status: "unavailable"` with its reason and never as `fetched`. Nothing synthetic is ever
written here.

`SIH26_INGEST_NETWORK=off` disables every network request (the test suite sets it); each product
that would have needed the network is then recorded unavailable with that reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
import yaml
from pydantic import BaseModel, ConfigDict, Field
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from backend.shared.grid import FLOAT_NODATA, build_farfield_grid
from backend.shared.site_config import SiteConfig

from . import dem as dem_mod
from . import discharge, download, hydrobasins

log = logging.getLogger("m1.ingest")

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = REPO_ROOT / "config" / "m1_ingestion.yaml"
NETWORK_ENV = "SIH26_INGEST_NETWORK"
CONTRACT_VERSION = "0.3.0"
#: Coverage tolerance when checking that a cached raster spans the far-field bbox (deg, ~1 m).
COVER_TOL_DEG = 1e-5
WORLDCOVER_NODATA = 0

Event = Callable[[str], None]


class IngestionError(RuntimeError):
    """A failure the onboarding job reports with its own contract §2.7 error code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.job_error_code = code
        self.job_error_details = details or {}


# --- settings ----------------------------------------------------------------------------------
class _HydroBasins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: int = Field(default=12, ge=1, le=12)
    required: bool = False


class _Discharge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    required: bool = False
    start_date: str = "1984-01-01"
    timeout_s: float = Field(default=60.0, gt=0)


class IngestionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dem_products: list[str] = ["copernicus_glo30", "srtm_gl1"]
    dem_preference: list[str] = ["copernicus_glo30", "srtm_gl1", "cartodem"]
    max_dem_void_fraction: float = Field(default=0.05, ge=0, le=1)
    #: cap (km^2) on the far-field DEM/landcover download request (bbox + margin), checked before
    #: any download. An engineering limit (RAM/disk/API), not a site fact.
    max_request_area_km2: float = Field(default=25000.0, gt=0)
    min_landcover_valid_fraction: float = Field(default=0.95, ge=0, le=1)
    worldcover_base_url: str = download.WORLDCOVER_BASE_URL
    hydrobasins: _HydroBasins = _HydroBasins()
    discharge: _Discharge = _Discharge()


def load_settings(path: Path | None = None) -> IngestionSettings:
    path = Path(path or SETTINGS_PATH)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    settings = IngestionSettings.model_validate(raw or {})
    unknown = set(settings.dem_products) - set(download.OPENTOPOGRAPHY_PRODUCTS)
    if unknown:
        raise ValueError(f"{path}: dem_products {sorted(unknown)} are not downloadable products")
    return settings


def network_enabled() -> bool:
    return os.environ.get(NETWORK_ENV, "on").strip().lower() not in {"off", "0", "false", "no"}


# --- 1. readiness ------------------------------------------------------------------------------
def missing_terrain_fields(cfg: SiteConfig) -> list[str]:
    """Every site-config field M1 cannot run without, in config order."""
    ff, nf = cfg.domains.far_field, cfg.domains.near_field
    checks = [
        ("crs.utm_epsg", cfg.crs.utm_epsg.value),
        ("domains.far_field.bbox", ff.bbox.value),
        ("domains.far_field.grid_resolution", ff.grid_resolution.value),
        ("domains.near_field.bbox", nf.bbox.value),
        ("domains.near_field.grid_resolution", nf.grid_resolution.value),
        (f"dams[0] ({cfg.dams[0].id}).breach_location", cfg.dams[0].breach_location.value),
    ]
    return [name for name, value in checks if value is None]


def check_terrain_readiness(cfg: SiteConfig) -> None:
    missing = missing_terrain_fields(cfg)
    if missing:
        raise IngestionError(
            "terrain_inputs_incomplete",
            f"site '{cfg.site.id}' cannot be ingested or terrain-processed: {len(missing)} required "
            f"config field(s) are empty: {', '.join(missing)}. Fill them in (a placeholder value is fine).",
            {"site_id": cfg.site.id, "missing_fields": missing},
        )


# --- provenance helpers --------------------------------------------------------------------------
def _read_provenance(raw_dir: Path) -> dict:
    path = raw_dir / "provenance.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _record(raw_dir: Path, key: str, entry: dict) -> None:
    download._merge_provenance(raw_dir, key, entry)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- 2. cache validation ------------------------------------------------------------------------
def validate_cached_raster(path: Path, bbox: list[float], entry: dict | None) -> str | None:
    """`None` if the cached raster at `path` is usable for `bbox` (EPSG:4326 far-field bbox),
    else the reason it is not: unreadable, checksum mismatch, or not covering the requested area."""
    try:
        with rasterio.open(path) as ds:
            if ds.crs is None:
                return "the cached raster has no CRS"
            west, south, east, north = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
    except Exception as e:  # noqa: BLE001
        return f"the cached file is not a readable raster ({e})"
    if entry and entry.get("sha256") and entry["sha256"] != _sha256(path):
        return "the cached file's checksum does not match its provenance entry"
    w, s, e, n = bbox
    if not (west <= w + COVER_TOL_DEG and south <= s + COVER_TOL_DEG and east >= e - COVER_TOL_DEG and north >= n - COVER_TOL_DEG):
        return (f"the cached raster covers [{west:.5f}, {south:.5f}, {east:.5f}, {north:.5f}] but the site requests "
                f"[{w}, {s}, {e}, {n}]")
    return None


# --- 3. DEM candidates ---------------------------------------------------------------------------
def _ensure_dem(cfg: SiteConfig, product: str, raw_dir: Path, bbox: list[float], network: bool,
                client, event: Event) -> dict:
    """Cached-and-valid, freshly fetched, or unavailable (never fake). Returns a status dict."""
    path = raw_dir / f"dem_{product}.tif"
    entry = _read_provenance(raw_dir).get(f"dem_{product}")
    force = False
    if path.exists():
        problem = validate_cached_raster(path, bbox, entry)
        if problem is None:
            event(f"dem {product}: cached file validated against the requested area")
            return {"product": product, "status": "cached", "file": path.name}
        event(f"dem {product}: cached file rejected ({problem})")
        force = True
        if not network:
            return {"product": product, "status": "unavailable",
                    "reason": f"cached file rejected ({problem}) and network ingestion is disabled ({NETWORK_ENV}=off)"}
    if not network:
        reason = f"not downloaded: network ingestion is disabled ({NETWORK_ENV}=off)"
        _record(raw_dir, f"dem_{product}", {"status": "unavailable", "dataset": product, "reason": reason,
                                             "checked_at": datetime.now(timezone.utc).isoformat()})
        return {"product": product, "status": "unavailable", "reason": reason}
    try:
        fetched = download.fetch_opentopography(cfg, product, raw_dir, client=client, force=force)
    except download.DownloadError as e:
        reason = str(e)  # never contains the API key (download.py redacts it)
        if not path.exists():
            _record(raw_dir, f"dem_{product}", {"status": "unavailable", "dataset": product, "reason": reason,
                                                 "checked_at": datetime.now(timezone.utc).isoformat()})
        event(f"dem {product}: unavailable ({reason})")
        return {"product": product, "status": "unavailable", "reason": reason}
    event(f"dem {product}: fetched")
    return {"product": product, "status": "fetched", "file": fetched["file"]}


def _cartodem_candidate(raw_dir: Path, bbox: list[float]) -> dict | None:
    """CartoDEM only when an operator supplied it explicitly (D1)."""
    path = raw_dir / "dem_cartodem.tif"
    entry = _read_provenance(raw_dir).get("dem_cartodem")
    if not path.exists() or not entry or entry.get("status") != "fetched":
        return None
    problem = validate_cached_raster(path, bbox, entry)
    if problem:
        return {"product": "cartodem", "status": "unavailable", "reason": f"supplied file rejected ({problem})"}
    return {"product": "cartodem", "status": "cached", "file": path.name}


# --- 4. land cover -------------------------------------------------------------------------------
def landcover_valid_fraction(path: Path, bbox: list[float]) -> float:
    with rasterio.open(path) as ds:
        w, s, e, n = transform_bounds("EPSG:4326", ds.crs, *bbox, densify_pts=21)
        window = from_bounds(w, s, e, n, transform=ds.transform).round_offsets().round_lengths()
        arr = ds.read(1, window=window, boundless=True, fill_value=WORLDCOVER_NODATA)
        nodata = ds.nodata if ds.nodata is not None else WORLDCOVER_NODATA
    return float(np.mean(arr != nodata)) if arr.size else 0.0


def _ensure_landcover(cfg: SiteConfig, raw_dir: Path, bbox: list[float], settings: IngestionSettings,
                      network: bool, worldcover_base_url: str, client, event: Event) -> dict:
    path = raw_dir / "landcover_esa_worldcover.tif"
    entry = _read_provenance(raw_dir).get("landcover_esa_worldcover")
    status, force = "cached", False
    if path.exists():
        problem = validate_cached_raster(path, bbox, entry)
        if problem:
            event(f"landcover: cached file rejected ({problem})")
            force, status = True, None
    if not path.exists() or force:
        if not network:
            raise IngestionError("terrain_failed",
                                 f"ESA WorldCover land cover is missing for site '{cfg.site.id}' and network ingestion "
                                 f"is disabled ({NETWORK_ENV}=off)", {"product": "landcover_esa_worldcover"})
        try:
            download.fetch_worldcover(cfg, raw_dir, base_url=worldcover_base_url, force=force, client=client)
        except download.DownloadError as e:
            raise IngestionError("terrain_failed", f"ESA WorldCover download failed: {e}",
                                 {"product": "landcover_esa_worldcover"}) from None
        status = "fetched"
    fraction = landcover_valid_fraction(path, bbox)
    if fraction < settings.min_landcover_valid_fraction:
        raise IngestionError(
            "terrain_failed",
            f"ESA WorldCover covers only {fraction:.1%} of the far-field area (minimum "
            f"{settings.min_landcover_valid_fraction:.0%}, config/m1_ingestion.yaml) -- partial land cover is not used",
            {"product": "landcover_esa_worldcover", "valid_fraction": fraction})
    event(f"landcover: {status}, {fraction:.1%} of the far-field area valid")
    return {"status": status, "valid_fraction": fraction}


# --- 5. DEM comparison and selection --------------------------------------------------------------
def compare_and_select(cfg: SiteConfig, raw_dir: Path, candidates: list[dict], settings: IngestionSettings) -> dict:
    grid = build_farfield_grid(cfg)
    usable = [c for c in candidates if c["status"] in ("cached", "fetched")]
    rows, arrays = [], {}
    for c in candidates:
        row = {"product": c["product"], "status": c["status"]}
        if c in usable:
            arr = dem_mod.load_dem(raw_dir / c["file"], grid)
            valid = arr != FLOAT_NODATA
            arrays[c["product"]] = (arr, valid)
            row.update({
                "file": c["file"], "void_fraction": float(1.0 - valid.mean()),
                "elevation_m": ({"min": float(arr[valid].min()), "max": float(arr[valid].max()),
                                  "mean": float(arr[valid].mean())} if valid.any() else None),
                "vertical_datum": _read_provenance(raw_dir).get(f"dem_{c['product']}", {}).get("vertical_datum"),
            })
        else:
            row["reason"] = c.get("reason")
        rows.append(row)
    measured = [r for r in rows if "void_fraction" in r]
    if not measured:
        reasons = {r["product"]: r.get("reason") for r in rows}
        raise IngestionError("terrain_failed",
                             "no DEM candidate is available: " + "; ".join(f"{p}: {why}" for p, why in reasons.items()),
                             {"candidates": reasons})
    order = {p: i for i, p in enumerate(settings.dem_preference)}
    measured.sort(key=lambda r: order.get(r["product"], len(order)))
    passing = [r for r in measured if r["void_fraction"] <= settings.max_dem_void_fraction]
    chosen = passing[0] if passing else min(measured, key=lambda r: (r["void_fraction"], order.get(r["product"], 99)))
    sel_arr, sel_valid = arrays[chosen["product"]]
    for r in measured:
        if r["product"] == chosen["product"]:
            continue
        arr, valid = arrays[r["product"]]
        both = valid & sel_valid
        diff = (arr - sel_arr)[both]
        r["difference_vs_selected_m"] = ({"mean": float(diff.mean()), "mean_abs": float(np.abs(diff).mean()),
                                          "std": float(diff.std()), "n_cells": int(both.sum())} if both.any() else None)
    comparison = {
        "contract_version": CONTRACT_VERSION, "site_id": cfg.site.id,
        "grid": {"grid_id": grid.grid_id, "crs_epsg": grid.crs_epsg, "cell_size_m": grid.cell_size_m, "shape": list(grid.shape)},
        "rule": {"preference": settings.dem_preference, "max_void_fraction": settings.max_dem_void_fraction,
                 "description": "first candidate in preference order whose void fraction is within the limit; "
                                "if none, the lowest-void candidate (threshold_met: false)"},
        "candidates": rows, "selected": chosen["product"], "threshold_met": bool(passing),
        "note": "difference_vs_selected_m mixes vertical datums when they differ (SRTM EGM96, GLO-30 EGM2008)",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (raw_dir / "dem_comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    return comparison


# --- orchestration -----------------------------------------------------------------------------
def ingest_site(cfg: SiteConfig, raw_dir: str | Path, *, settings: IngestionSettings | None = None,
                client=None, worldcover_base_url: str | None = None,
                discharge_client=None, event: Event | None = None) -> dict:
    """Ingest every Feature 2 product for `cfg` into `raw_dir`; return a summary with the
    selected DEM. Raises IngestionError (job error code attached) on a blocking failure."""
    settings = settings or load_settings()
    event = event or (lambda msg: log.info(msg))
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    check_terrain_readiness(cfg)
    try:
        area = download.check_request_area(cfg, settings.max_request_area_km2)
    except download.RequestAreaTooLarge as e:
        raise IngestionError("terrain_failed", str(e), {
            "site_id": cfg.site.id, "reason": "request_area_too_large",
            "request_area_km2": e.area_km2, "max_request_area_km2": e.max_area_km2}) from None
    event(f"download request area: {area} km^2 (cap {settings.max_request_area_km2} km^2)")
    bbox = [float(v) for v in cfg.domains.far_field.bbox.value]
    network = network_enabled()

    candidates = [_ensure_dem(cfg, p, raw_dir, bbox, network, client, event) for p in settings.dem_products]
    carto = _cartodem_candidate(raw_dir, bbox)
    if carto:
        candidates.append(carto)
    landcover = _ensure_landcover(cfg, raw_dir, bbox, settings, network,
                                  worldcover_base_url or settings.worldcover_base_url, client, event)
    comparison = compare_and_select(cfg, raw_dir, candidates, settings)
    event(f"dem selected: {comparison['selected']} (threshold met: {comparison['threshold_met']})")

    catchment = hydrobasins.fetch_catchment(cfg, raw_dir, level=settings.hydrobasins.level, network=network)
    _record(raw_dir, "hydrobasins_catchment", catchment)
    event(f"hydrobasins: {catchment['status']}" + (f" ({catchment['reason']})" if catchment["status"] != "fetched" else ""))
    if catchment["status"] != "fetched" and settings.hydrobasins.required:
        raise IngestionError("terrain_failed", f"HydroBASINS catchment is required but unavailable: {catchment['reason']}",
                             {"product": "hydrobasins_catchment"})

    flow = _ingest_discharge(cfg, raw_dir, settings, network, discharge_client)
    event(f"discharge: {flow['status']}" + (f" ({flow['reason']})" if flow["status"] != "fetched" else ""))
    if flow["status"] != "fetched" and settings.discharge.enabled and settings.discharge.required:
        raise IngestionError("terrain_failed", f"river discharge is required but unavailable: {flow['reason']}",
                             {"product": "discharge_glofas"})

    return {"selected_dem": comparison["selected"], "dem_comparison": comparison, "landcover": landcover,
            "hydrobasins": catchment["status"], "discharge": flow["status"]}


def _ingest_discharge(cfg: SiteConfig, raw_dir: Path, settings: IngestionSettings, network: bool, client) -> dict:
    base = {"dataset": discharge.DATASET, "source": discharge.SOURCE_ID, "service_url": discharge.SERVICE_URL,
            "checked_at": datetime.now(timezone.utc).isoformat()}
    if not settings.discharge.enabled:
        entry = {**base, "status": "unavailable", "reason": "disabled in config/m1_ingestion.yaml (discharge.enabled: false)"}
    elif not network:
        entry = {**base, "status": "unavailable", "reason": f"network ingestion is disabled ({NETWORK_ENV}=off)"}
    else:
        try:
            entry = discharge.fetch_discharge(cfg, raw_dir, start_date=settings.discharge.start_date,
                                              client=client, timeout_s=settings.discharge.timeout_s)
        except discharge.DischargeUnavailable as e:
            entry = {**base, "status": "unavailable", "reason": str(e)}
    if entry["status"] != "fetched" and (raw_dir / discharge.OUTPUT_NAME).exists():
        # A previous fetch is kept on disk, but its provenance stays the fetched one: only replace
        # the entry when nothing valid was ever fetched.
        if _read_provenance(raw_dir).get("discharge_glofas", {}).get("status") == "fetched":
            return {**entry, "kept_previous_fetch": True}
    _record(raw_dir, "discharge_glofas", entry)
    return entry

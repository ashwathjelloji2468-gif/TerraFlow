"""M1-1: download raw DEM and landcover candidates for a site's far-field bbox.

Fetches every DEM candidate the team might pick from, into `data/<site_id>/raw/`, each in its
native CRS/resolution, untouched. The DEM used for `terrain/dem.tif` is chosen from the M1-2
comparison report (void %, difference maps, valley profiles) — this module does not choose one,
and the site config has no `dem.source` field yet (`docs/handoff_contract.md` §3.1 "not
implemented", `docs/decisions.md` 2026-09-25).

Products:
- `srtm_gl1`, `copernicus_glo30` — via the OpenTopography Global DEM API (`fetch_opentopography`).
  Needs an API key: `OPENTOPOGRAPHY_API_KEY` in the environment or the repo `.env`
  (CLAUDE.md rule 12 — the key is never logged, written to provenance, or included in an error
  message).
- `worldcover` — ESA WorldCover 10 m 2021 v200, mosaicked from its public COG tiles on S3, no key
  needed (`fetch_worldcover`).
- CartoDEM has no public bulk-download API (`docs/data_sources.md` src_037); a folder of manually
  downloaded tiles is mosaicked with `mosaic_cartodem`.

Every product writes `data/<site_id>/raw/provenance.json` (merged, one key per product) recording
dataset, source id, request bbox, native CRS/resolution/vertical datum, licence, checksum and
fetch time — the same pattern as `backend/m6_impact/exposure_worldpop.py`.

CLI: `python -m backend.m1_terrain.download <site_id> [--products srtm_gl1,copernicus_glo30,worldcover]
[--cartodem-dir DIR --cartodem-version V --cartodem-vertical-datum D] [--force]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import rasterio
from rasterio.merge import merge as rio_merge

from backend.shared.site_config import SiteConfig, default_data_dir, load_site_config

log = logging.getLogger("m1.download")

# httpx logs "HTTP Request: GET <url> ..." at INFO level by default, and <url> includes
# API_Key=... in the query string — that would leak the key into any run with INFO logging
# enabled (this module's own CLI sets basicConfig(level=INFO)). CLAUDE.md rule 12.
logging.getLogger("httpx").setLevel(logging.WARNING)

_API_KEY_RE = re.compile(r"(API_Key=)[^&]*")


def _redact_url(url: str) -> str:
    """`url` with any `API_Key=...` query value replaced by `***`, for the rare case we log a
    request URL ourselves (rule 12 — the real key must never reach a log line or an exception)."""
    return _API_KEY_RE.sub(r"\1***", url)


CONTRACT_VERSION = "0.3.0"
# The data/ root is resolved at call time from `SIH26_DATA_DIR` (else `<repo>/data`), the same
# rule the API and worker use (`backend.shared.site_config.default_data_dir`).
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# ~1 km, so bilinear resampling onto the canonical grid (backend/shared/grid.py) has full
# coverage right up to the far-field grid's edge.
BBOX_MARGIN_DEG = 0.01

OPENTOPOGRAPHY_URL = "https://portal.opentopography.org/API/globaldem"
OPENTOPOGRAPHY_KEY_ENV = "OPENTOPOGRAPHY_API_KEY"
OPENTOPOGRAPHY_LICENSE = "See https://portal.opentopography.org/datasetMetadata for each dataset's licence"
OPENTOPOGRAPHY_PRODUCTS: dict[str, dict] = {
    "srtm_gl1": {"demtype": "SRTMGL1", "source_id": "src_033", "vertical_datum": "EGM96", "native_resolution_m": 30},
    "copernicus_glo30": {"demtype": "COP30", "source_id": "src_034", "vertical_datum": "EGM2008", "native_resolution_m": 30},
}

WORLDCOVER_BASE_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
WORLDCOVER_SOURCE_ID = "src_036"
WORLDCOVER_LICENSE = "CC BY 4.0, https://esa-worldcover.org"
WORLDCOVER_TILE_DEG = 3

CARTODEM_SOURCE_ID = "src_037"

Bbox = tuple[float, float, float, float]  # (west, south, east, north), EPSG:4326


class DownloadError(RuntimeError):
    """A DEM/landcover product could not be fetched, mosaicked or written."""


class RequestAreaTooLarge(DownloadError):
    """The far-field download request (bbox + margin) exceeds the configured area cap."""

    def __init__(self, message: str, area_km2: float, max_area_km2: float):
        super().__init__(message)
        self.area_km2, self.max_area_km2 = area_km2, max_area_km2


#: Mean Earth radius (IUGG R1), km -- used only to size a download request, not for any physics.
EARTH_RADIUS_KM = 6371.0088


# =============================================================================
# Shared helpers
# =============================================================================


def site_bbox_with_margin(cfg: SiteConfig, margin_deg: float = BBOX_MARGIN_DEG) -> Bbox:
    """The site's far-field bbox (contract §3.1 `domains.far_field.bbox`), buffered by
    `margin_deg` and rounded to 6 decimal places (~0.11 m at the equator — far finer than any
    DEM's resolution, so this doesn't change what's requested; it just keeps the request, its
    logs and its provenance free of float noise like `26.639999999999997`, and reproducible run
    to run). Raises if the bbox is still a placeholder."""
    bbox = cfg.domains.far_field.bbox.value
    if bbox is None:
        raise DownloadError(
            f"site '{cfg.site.id}': domains.far_field.bbox is a placeholder (null) — "
            "fill it in before downloading DEM/landcover data"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        round(min_lon - margin_deg, 6), round(min_lat - margin_deg, 6),
        round(max_lon + margin_deg, 6), round(max_lat + margin_deg, 6),
    )


def bbox_area_km2(bbox: Bbox) -> float:
    """Area (km^2) of a lon/lat box `(west, south, east, north)` on a sphere of radius
    `EARTH_RADIUS_KM`: R^2 * dlon * (sin(north) - sin(south)). Deterministic, no projection."""
    west, south, east, north = bbox
    return EARTH_RADIUS_KM ** 2 * math.radians(east - west) * \
        (math.sin(math.radians(north)) - math.sin(math.radians(south)))


def check_request_area(cfg: SiteConfig, max_area_km2: float, margin_deg: float = BBOX_MARGIN_DEG) -> float:
    """Area (km^2) of the bbox actually requested for `cfg` (far-field bbox + `margin_deg`);
    raises `RequestAreaTooLarge` BEFORE any download when it exceeds `max_area_km2`
    (`config/m1_ingestion.yaml` `max_request_area_km2`)."""
    area = round(bbox_area_km2(site_bbox_with_margin(cfg, margin_deg)), 1)
    if area > max_area_km2:
        raise RequestAreaTooLarge(
            f"site '{cfg.site.id}': the far-field download request covers {area} km^2, above the "
            f"configured cap of {max_area_km2} km^2 (config/m1_ingestion.yaml max_request_area_km2); "
            "shrink domains.far_field.bbox or raise the cap deliberately", area, max_area_km2)
    return area


def opentopography_api_key() -> str:
    """`OPENTOPOGRAPHY_API_KEY` from the environment, else parsed from the repo `.env`.
    Never returns a placeholder; raises with the variable name, never any value, if missing."""
    key = os.environ.get(OPENTOPOGRAPHY_KEY_ENV)
    if key:
        return key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == OPENTOPOGRAPHY_KEY_ENV:
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise DownloadError(
        f"{OPENTOPOGRAPHY_KEY_ENV} is not set (checked the environment and {ENV_FILE}). "
        "Get a free key at https://opentopography.org/ and set it before downloading DEMs."
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _merge_provenance(raw_dir: Path, key: str, entry: dict) -> None:
    path = raw_dir / "provenance.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing[key] = entry
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_raster(tmp_path: Path, out_path: Path, write_fn) -> None:
    """Call `write_fn(tmp_path)` to produce the file, then atomically rename it to `out_path`.
    Removes `tmp_path` on any failure, including one raised while `write_fn` runs."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_fn(tmp_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.rename(out_path)


# =============================================================================
# OpenTopography (SRTM GL1, Copernicus GLO-30)
# =============================================================================


def fetch_opentopography(cfg: SiteConfig, product: str, raw_dir: Path, *,
                          client: httpx.Client | None = None, force: bool = False) -> dict:
    """Download one DEM product for `cfg`'s far-field bbox (+ margin) from the OpenTopography
    Global DEM API into `raw_dir/dem_<product>.tif`. `product` is a key of
    `OPENTOPOGRAPHY_PRODUCTS` (`srtm_gl1` | `copernicus_glo30`)."""
    if product not in OPENTOPOGRAPHY_PRODUCTS:
        raise ValueError(f"unknown OpenTopography product {product!r}; choose from {sorted(OPENTOPOGRAPHY_PRODUCTS)}")
    spec = OPENTOPOGRAPHY_PRODUCTS[product]
    raw_dir = Path(raw_dir)
    out_path = raw_dir / f"dem_{product}.tif"
    if out_path.exists() and not force:
        log.info("%s already exists, skipping", out_path)
        return {"file": out_path.name, "status": "skipped_existing"}

    west, south, east, north = site_bbox_with_margin(cfg)
    params = {
        "demtype": spec["demtype"], "south": south, "north": north,
        "west": west, "east": east, "outputFormat": "GTiff",
        "API_Key": opentopography_api_key(),
    }
    safe_params = {k: v for k, v in params.items() if k != "API_Key"}  # never log/raise with the key

    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    owns_client = client is None
    client = client or httpx.Client(timeout=180.0, follow_redirects=True)
    result: dict = {}
    try:

        def _download(tmp: Path) -> None:
            try:
                request = client.build_request("GET", OPENTOPOGRAPHY_URL, params=params)
                log.debug("GET %s", _redact_url(str(request.url)))
                resp = client.send(request, stream=True)
                try:
                    content_type = resp.headers.get("content-type", "")
                    if resp.status_code != 200 or "json" in content_type or content_type.startswith("text/"):
                        body = b"".join(resp.iter_bytes())[:500]
                        raise DownloadError(
                            f"OpenTopography request for {product} failed "
                            f"(status {resp.status_code}, params {safe_params}): "
                            f"{body.decode('utf-8', 'replace')}"
                        )
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1 << 20):
                            f.write(chunk)
                finally:
                    resp.close()
            except httpx.HTTPError as e:
                raise DownloadError(
                    f"OpenTopography request for {product} failed (params {safe_params}): "
                    f"{type(e).__name__}"
                ) from None
            # Validate before it's renamed into place — a bad/empty response should never look cached.
            with rasterio.open(tmp) as ds:
                result["native_crs"] = str(ds.crs)

        _write_raster(tmp_path, out_path, _download)
    finally:
        if owns_client:
            client.close()

    entry = {
        "file": out_path.name, "status": "fetched",
        "dataset": product, "demtype": spec["demtype"], "source": spec["source_id"],
        "service_url": OPENTOPOGRAPHY_URL, "request_bbox_deg": [west, south, east, north],
        "native_crs": result["native_crs"],
        "native_resolution_m": spec["native_resolution_m"], "vertical_datum": spec["vertical_datum"],
        "license": OPENTOPOGRAPHY_LICENSE, "sha256": _sha256(out_path), "size_bytes": out_path.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "contract_version": CONTRACT_VERSION,
    }
    _merge_provenance(raw_dir, f"dem_{product}", entry)
    return entry


# =============================================================================
# ESA WorldCover
# =============================================================================


def worldcover_tiles(bbox: Bbox, tile_deg: int = WORLDCOVER_TILE_DEG) -> list[str]:
    """WorldCover tile names (SW-corner naming, e.g. `N24E087`) covering `bbox`."""
    west, south, east, north = bbox
    lat0 = math.floor(south / tile_deg) * tile_deg
    lat1 = math.floor(north / tile_deg) * tile_deg
    lon0 = math.floor(west / tile_deg) * tile_deg
    lon1 = math.floor(east / tile_deg) * tile_deg
    tiles = []
    lat = lat0
    while lat <= lat1:
        lon = lon0
        while lon <= lon1:
            tiles.append(_tile_name(lat, lon))
            lon += tile_deg
        lat += tile_deg
    return tiles


def _tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def _worldcover_source_path(base_url: str, tile: str) -> str:
    fname = f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
    if base_url.startswith("http://") or base_url.startswith("https://"):
        return f"/vsicurl/{base_url.rstrip('/')}/{fname}"
    return str(Path(base_url) / fname)


def _worldcover_tile_state(base_url: str, tile: str, client: httpx.Client | None) -> str:
    """`"present"` or `"absent"` (the tile does not exist: ESA publishes no tile over open
    ocean). Anything else -- a timeout, a refused connection, a 5xx or 403 -- raises
    DownloadError: a network failure must never look like "no tile here" (Feature 2)."""
    fname = f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return "present" if (Path(base_url) / fname).is_file() else "absent"
    url = f"{base_url.rstrip('/')}/{fname}"
    owns = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        resp = client.head(url)
    except httpx.HTTPError as e:
        raise DownloadError(f"ESA WorldCover tile {tile} could not be checked ({type(e).__name__}): {url}") from None
    finally:
        if owns:
            client.close()
    if resp.status_code == 200:
        return "present"
    if resp.status_code == 404:
        return "absent"
    raise DownloadError(f"ESA WorldCover tile {tile} request failed (HTTP {resp.status_code}): {url}")


def fetch_worldcover(cfg: SiteConfig, raw_dir: Path, *, base_url: str = WORLDCOVER_BASE_URL,
                      force: bool = False, client: httpx.Client | None = None) -> dict:
    """Mosaic ESA WorldCover 10 m tiles covering `cfg`'s far-field bbox (+ margin) into
    `raw_dir/landcover_esa_worldcover.tif`. `base_url` may be a local directory (tests).

    A tile that does not exist (open ocean) is skipped and listed in `tiles_skipped`; a tile that
    exists but cannot be checked or opened is a DownloadError, never a silent gap."""
    raw_dir = Path(raw_dir)
    out_path = raw_dir / "landcover_esa_worldcover.tif"
    if out_path.exists() and not force:
        log.info("%s already exists, skipping", out_path)
        return {"file": out_path.name, "status": "skipped_existing"}

    bbox = site_bbox_with_margin(cfg)
    tiles = worldcover_tiles(bbox)
    used_tiles, skipped_tiles, datasets = [], [], []
    try:
        for tile in tiles:
            if _worldcover_tile_state(base_url, tile, client) == "absent":
                log.warning("ESA WorldCover tile %s does not exist (no land in this tile); skipped", tile)
                skipped_tiles.append(tile)
                continue
            path = _worldcover_source_path(base_url, tile)
            try:
                datasets.append(rasterio.open(path))
            except Exception as e:
                raise DownloadError(f"ESA WorldCover tile {tile} exists but could not be read: {e}") from None
            used_tiles.append(tile)
        if not datasets:
            raise DownloadError(f"no ESA WorldCover tiles could be opened for bbox {bbox} (tried {tiles})")

        merged, transform = rio_merge(datasets, bounds=bbox)
        profile = datasets[0].profile.copy()
    finally:
        for ds in datasets:
            ds.close()

    profile.update(height=merged.shape[1], width=merged.shape[2], transform=transform,
                    tiled=True, blockxsize=256, blockysize=256, compress="lzw")
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    _write_raster(tmp_path, out_path, lambda tmp: _write_array(tmp, merged, profile))

    entry = {
        "file": out_path.name, "status": "fetched", "dataset": "esa_worldcover_2021_v200",
        "source": WORLDCOVER_SOURCE_ID, "service_url": base_url, "tiles_used": used_tiles,
        "tiles_skipped": skipped_tiles, "request_bbox_deg": list(bbox), "native_resolution_m": 10,
        "license": WORLDCOVER_LICENSE, "sha256": _sha256(out_path), "size_bytes": out_path.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "contract_version": CONTRACT_VERSION,
    }
    _merge_provenance(raw_dir, "landcover_esa_worldcover", entry)
    return entry


def _write_array(path: Path, array, profile: dict) -> None:
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array)


# =============================================================================
# CartoDEM (manual tiles)
# =============================================================================


def mosaic_cartodem(cfg: SiteConfig, tile_dir: str | Path, raw_dir: Path, *,
                     version: str | None = None, vertical_datum: str | None = None,
                     force: bool = False) -> dict:
    """Mosaic manually downloaded CartoDEM `.tif` tiles from `tile_dir` (no public bulk-download
    API, `docs/data_sources.md` src_037) into `raw_dir/dem_cartodem.tif`, clipped to the site's
    far-field bbox (+ margin). `version`/`vertical_datum` have no default — pass them once known,
    or provenance records them as `null` with a logged warning (contract §1.3)."""
    raw_dir = Path(raw_dir)
    out_path = raw_dir / "dem_cartodem.tif"
    if out_path.exists() and not force:
        log.info("%s already exists, skipping", out_path)
        return {"file": out_path.name, "status": "skipped_existing"}

    tile_dir = Path(tile_dir)
    tile_paths = sorted(p for p in tile_dir.iterdir() if p.suffix.lower() == ".tif")
    if not tile_paths:
        raise DownloadError(f"no .tif tiles found in {tile_dir}")
    if version is None or vertical_datum is None:
        log.warning(
            "mosaic_cartodem: version and/or vertical_datum not given; provenance will record "
            "null for whichever is missing (docs/data_sources.md src_037 needs both before any "
            "value derived from this DEM can be status: sourced)"
        )

    bbox = site_bbox_with_margin(cfg)
    datasets = [rasterio.open(p) for p in tile_paths]
    try:
        crs_set = {str(ds.crs) for ds in datasets}
        if len(crs_set) > 1:
            raise DownloadError(f"CartoDEM tiles have mixed CRS {sorted(crs_set)} — reproject them to a common CRS first")
        native_crs = next(iter(crs_set))
        merged, transform = rio_merge(datasets, bounds=bbox)
        profile = datasets[0].profile.copy()
    finally:
        for ds in datasets:
            ds.close()

    profile.update(height=merged.shape[1], width=merged.shape[2], transform=transform,
                    tiled=True, blockxsize=256, blockysize=256, compress="lzw")
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    _write_raster(tmp_path, out_path, lambda tmp: _write_array(tmp, merged, profile))

    entry = {
        "file": out_path.name, "status": "fetched", "dataset": "cartodem", "source": CARTODEM_SOURCE_ID,
        "service_url": None, "tile_files": [p.name for p in tile_paths], "request_bbox_deg": list(bbox),
        "native_crs": native_crs, "version": version, "vertical_datum": vertical_datum, "license": None,
        "sha256": _sha256(out_path), "size_bytes": out_path.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "contract_version": CONTRACT_VERSION,
    }
    _merge_provenance(raw_dir, "dem_cartodem", entry)
    return entry


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_id", help="a registered site id (bundled sites/<id>.yaml or onboarded data/<id>/config/<id>.yaml)")
    parser.add_argument("--products", default="srtm_gl1,copernicus_glo30,worldcover",
                         help="comma-separated subset of srtm_gl1, copernicus_glo30, worldcover")
    parser.add_argument("--cartodem-dir", default=None, help="folder of manually downloaded CartoDEM .tif tiles")
    parser.add_argument("--cartodem-version", default=None, help="CartoDEM version/release (docs/data_sources.md src_037)")
    parser.add_argument("--cartodem-vertical-datum", default=None, help="CartoDEM vertical datum (docs/data_sources.md src_037)")
    parser.add_argument("--force", action="store_true", help="re-download even if the output file already exists")
    parser.add_argument("--data-dir", default=None, help="override the data/ root (default: $SIH26_DATA_DIR, else <repo>/data)")
    args = parser.parse_args(argv)

    cfg = load_site_config(args.site_id)
    raw_dir = Path(args.data_dir or default_data_dir()) / cfg.site.id / "raw"

    products = [p.strip() for p in args.products.split(",") if p.strip()]
    from .ingest import load_settings  # deferred: ingest imports this module
    try:
        check_request_area(cfg, load_settings().max_request_area_km2)
    except RequestAreaTooLarge as e:
        raise SystemExit(str(e)) from None
    known = set(OPENTOPOGRAPHY_PRODUCTS) | {"worldcover"}
    if unknown := sorted(set(products) - known):
        raise SystemExit(f"unknown product(s) {unknown}; choose from {sorted(known)}")

    for product in products:
        if product == "worldcover":
            entry = fetch_worldcover(cfg, raw_dir, force=args.force)
        else:
            entry = fetch_opentopography(cfg, product, raw_dir, force=args.force)
        log.info("%s: %s", product, entry["status"])

    if args.cartodem_dir:
        entry = mosaic_cartodem(cfg, args.cartodem_dir, raw_dir, version=args.cartodem_version,
                                 vertical_datum=args.cartodem_vertical_datum, force=args.force)
        log.info("cartodem: %s", entry["status"])

    return 0


if __name__ == "__main__":
    sys.exit(main())

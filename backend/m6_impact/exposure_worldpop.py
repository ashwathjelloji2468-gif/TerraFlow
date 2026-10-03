"""Download, clip and grid WorldPop population for a site's far-field bbox
(docs/handoff_contract.md §4.7 "Exposure inputs" table: `population.tif`,
"WorldPop persons per cell, resampled to far-field grid (sum-preserving)").

Dataset: WorldPop "Global 2000-2020, 1km, UN-adjusted" population counts
(https://hub.worldpop.org, CC BY 4.0). Chosen over the 100 m "constrained"
product because the 100 m national mosaic is ~530 MB and its server does not
honour HTTP Range requests (no partial/windowed download possible), while the
1 km UN-adjusted national mosaic is ~18 MB and downloads whole in seconds.
This means population is disaggregated uniformly within each ~1 km source
cell onto the (finer) far-field grid rather than following real building
footprints — an honest coarse approximation, noted in `provenance.json` and
`data_coverage_notes`, consistent with this project's "speed and honest
uncertainty over precision" brief (CLAUDE.md).

The national raster is cached once under `data/_cache/worldpop/` (shared
across sites, since India's mosaic covers every current site) and clipped
per-site into `data/<site_id>/raw/` before being resampled sum-preserving
onto the far-field canonical grid to make `exposure/population.tif`.

CLI: `python -m backend.m6_impact.exposure_worldpop <site_id>`
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.mask import mask
from shapely.geometry import box

from backend.shared.grid import build_farfield_grid, resample_to_grid, write_grid_raster
import re

from backend.shared.site_config import COUNTRY_ISO3_PATTERN, SiteConfig, load_site_config

log = logging.getLogger("m6.exposure_worldpop")

WORLDPOP_DATASET = "Global_2000_2020_1km_UNadj"
WORLDPOP_YEAR = 2020
WORLDPOP_LICENSE = "Creative Commons Attribution 4.0 International (CC BY 4.0), https://www.worldpop.org"
# Feature 16: there is NO default country. The national raster comes from the site config's
# `site.country_iso3` or an explicit `--iso3`; it is never inferred from `site.region` or the bbox.


class CountryUnknown(ValueError):
    """No country is stated for the site and none was passed explicitly."""


def resolve_iso3(cfg: SiteConfig, iso3: str | None = None) -> tuple[str, str]:
    """(iso3, basis) for `cfg`: the explicit `iso3` (basis "explicit --iso3") or `site.country_iso3`
    (basis "site.country_iso3"). Both given and different -> ValueError; neither -> CountryUnknown."""
    configured = cfg.site.country_iso3
    if iso3 is not None:
        if not isinstance(iso3, str) or not re.fullmatch(COUNTRY_ISO3_PATTERN, iso3):
            raise ValueError(f"--iso3 {iso3!r} is not a 3-letter upper-case country code")
        if configured is not None and configured != iso3:
            raise ValueError(f"--iso3 {iso3} conflicts with site.country_iso3 {configured} for '{cfg.site.id}'")
        return iso3, "explicit --iso3"
    if configured is not None:
        return configured, "site.country_iso3"
    raise CountryUnknown(f"site '{cfg.site.id}' has no site.country_iso3; pass --iso3 explicitly "
                         "(the country is never assumed or inferred from region/bbox)")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def worldpop_url(iso3: str, year: int = WORLDPOP_YEAR) -> str:
    iso3_lower = iso3.lower()
    return (f"https://data.worldpop.org/GIS/Population/{WORLDPOP_DATASET}/{year}/{iso3}/"
            f"{iso3_lower}_ppp_{year}_1km_Aggregated_UNadj.tif")


def download_national_raster(cache_dir: Path, iso3: str, year: int = WORLDPOP_YEAR,
                              client: httpx.Client | None = None) -> Path:
    """Download the whole-country population mosaic to `cache_dir`, skipping if already cached.
    Downloads to a `.part` file first so a truncated/interrupted transfer never looks cached."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{iso3.lower()}_ppp_{year}_1km_Aggregated_UNadj.tif"
    if out_path.exists():
        log.info("worldpop national raster already cached: %s", out_path)
        return out_path

    url = worldpop_url(iso3, year)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    log.info("downloading %s", url)
    owns_client = client is None
    client = client or httpx.Client(timeout=300.0, follow_redirects=True)
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            client.close()
    tmp_path.rename(out_path)
    log.info("downloaded %s (%.1f MB)", out_path, out_path.stat().st_size / 1e6)
    return out_path


def clip_to_bbox(national_path: Path, bbox: list[float], out_path: Path) -> Path:
    """Clip the (EPSG:4326) national raster to `bbox` ([min_lon, min_lat, max_lon, max_lat]),
    values unchanged, and write it to `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(national_path) as src:
        clipped, transform = mask(src, [box(*bbox)], crop=True, nodata=src.nodata)
        profile = src.profile.copy()
        if not profile.get("tiled"):
            profile.pop("blockxsize", None)
        profile.update(height=clipped.shape[1], width=clipped.shape[2], transform=transform)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(clipped)
    return out_path


def resample_sum_preserving(clip_path: Path, cfg: SiteConfig, out_path: Path) -> tuple[Path, float, float]:
    """Resample the clipped raster onto the site's far-field grid with sum-preserving
    ('sum') resampling and write `population.tif`. Returns (path, source_sum, resampled_sum)
    so callers can sanity-check the two totals stay close."""
    grid = build_farfield_grid(cfg)
    with rasterio.open(clip_path) as src:
        source = src.read(1, masked=True)
        source_sum = float(np.ma.filled(source, 0.0).clip(min=0).sum())

    array = resample_to_grid(clip_path, grid, method="sum")
    valid = array != grid.nodata
    resampled_sum = float(array[valid].clip(min=0).sum())

    write_grid_raster(out_path, array, grid)
    return out_path, source_sum, resampled_sum


def _write_provenance(exposure_dir: Path, entry: dict) -> None:
    path = exposure_dir / "provenance.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing["population"] = entry
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch(cfg: SiteConfig, data_dir: Path = DATA_DIR, iso3: str | None = None) -> dict:
    bbox = cfg.domains.far_field.bbox.value
    if bbox is None:
        raise ValueError(f"site '{cfg.site.id}': domains.far_field.bbox is a placeholder (null) — "
                          "fill it in before fetching exposure data")

    exposure_path = data_dir / cfg.site.id / "exposure" / "population.tif"
    if exposure_path.exists():
        log.info("population: %s already exists, skipping", exposure_path)
        return {"file": exposure_path.name, "status": "skipped_existing"}

    iso3, country_basis = resolve_iso3(cfg, iso3)  # before any download or file write
    exposure_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = data_dir / "_cache" / "worldpop"
    raw_dir = data_dir / cfg.site.id / "raw"
    raw_clip_path = raw_dir / f"worldpop_{iso3.lower()}_ppp_{WORLDPOP_YEAR}_1km_unadj_clip.tif"

    national_path = download_national_raster(cache_dir, iso3=iso3, year=WORLDPOP_YEAR)
    if not raw_clip_path.exists():
        clip_to_bbox(national_path, bbox, raw_clip_path)
    else:
        log.info("raw clip already exists: %s", raw_clip_path)

    _, source_sum, resampled_sum = resample_sum_preserving(raw_clip_path, cfg, exposure_path)
    log.info("population: source sum %.0f persons, resampled sum %.0f persons", source_sum, resampled_sum)

    entry = {
        "file": exposure_path.name,
        "status": "fetched",
        "source": "WorldPop Global 2000-2020, 1km, UN-adjusted population counts",
        "url": worldpop_url(iso3, WORLDPOP_YEAR),
        "country_iso3": iso3,
        "country_basis": country_basis,
        "license": WORLDPOP_LICENSE,
        "year": WORLDPOP_YEAR,
        "native_resolution_m": 1000,
        "resampling": "sum-preserving ('sum') onto the far-field canonical grid; uniform "
                      "disaggregation within each ~1km source cell, not building-level detail",
        "bbox_deg": bbox,
        "raw_clip_file": str(raw_clip_path.relative_to(data_dir)),
        "source_sum_persons": source_sum,
        "resampled_sum_persons": resampled_sum,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_provenance(exposure_path.parent, entry)
    return entry


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_id", help="site id (a sites/<site_id>.yaml must exist)")
    parser.add_argument("--iso3", default=None,
                        help="WorldPop country code (ISO 3166-1 alpha-3). Required when the site config has "
                             "no site.country_iso3; there is no default")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="override the data/ root")
    args = parser.parse_args(argv)

    cfg = load_site_config(args.site_id)
    try:
        fetch(cfg, data_dir=Path(args.data_dir), iso3=args.iso3)
    except (CountryUnknown, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

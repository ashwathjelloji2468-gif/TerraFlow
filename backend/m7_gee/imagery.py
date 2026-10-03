"""M7 GEE event imagery (contract §4.8 `imagery/<event_id>_<pre|post>_<date>.png`).

Converts the pre-/post-event RGB GeoTIFFs an operator has already staged (referenced by
`sites/<site_id>.yaml` `events[].imagery_pre_event/imagery_post_event.source`, a repo-root-relative
path such as `cache/gee/teesta/teesta_pre_event.tif` -> its `..._rgb.tif` sibling) into
`data/<site_id>/gee/imagery/`: a full-resolution PNG, a small fallback PNG (<= `FALLBACK_MAX_PX` on
the long side, for slow connections), and a `manifest.json` recording each PNG's EPSG:4326 bounds
so the frontend can place it on the Leaflet map without re-reading the GeoTIFF.
`cache.load_layers()` reads the manifest to fill `GeeLayers.imagery` (contract §5.8).

Also supports a live refresh: `refresh(site_id)` tries to re-render the same pre-/post-event
composites from Earth Engine (needs `GEE_SERVICE_ACCOUNT_EMAIL` / `GEE_SERVICE_ACCOUNT_KEY_PATH`
in `.env`, or `earthengine authenticate`), overwriting the staged `_rgb.tif` files before
reconverting. Any failure (no credentials, no network, no scene near the date) is caught and the
existing on-disk PNGs are kept -- contract §4.8 "fallback/*.png ... if live and cache both fail".

`convert()` optionally takes the scene IDs the staged GeoTIFFs were exported from (there is nowhere
in `sites/<site_id>.yaml`'s `Event` to record them -- `imagery_pre_event`/`imagery_post_event` only
carry a date + file path, and `additionalProperties: false` on that schema object means adding a
field there is a contract change, not a code one). When given, they are merged into `gee_meta.json`
under an `"imagery"` entry, alongside the `lake_area`/`lake_latest`/`rainfall` entries `fetch.py`
already writes there (contract §4.8: `gee_meta.json` is "per product: dataset, scene_ids,
acquisition_dates, cloud_pct, fetched_at, source").

CLI: `python -m backend.m7_gee.imagery <site_id> [--data-dir DIR] [--refresh] [--ee-project PROJECT]
[--pre-scene-id ID ...] [--post-scene-id ID ...]`
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from backend.shared.site_config import Event, SiteConfig, load_site_config

from . import cache

log = logging.getLogger("m7.imagery")

REPO_ROOT = Path(__file__).resolve().parents[2]
FALLBACK_MAX_PX = 512
PHASES = ("pre", "post")
MANIFEST_NAME = "imagery/manifest.json"


@dataclass
class ImageryResult:
    site_id: str
    manifest_path: Path | None = None
    source: str = "cache"  # "live" | "cache" | "none" (nothing on disk either)
    errors: list[str] = field(default_factory=list)


def _event_for_imagery(cfg: SiteConfig) -> Event:
    """The first event with both `imagery_pre_event` and `imagery_post_event` set. Raises if
    none has both -- CLAUDE.md rule 3: no invented event facts."""
    for ev in cfg.events:
        if ev.imagery_pre_event.value and ev.imagery_post_event.value:
            return ev
    raise ValueError(
        f"site '{cfg.site.id}' has no event with both imagery_pre_event and imagery_post_event set"
    )


class UnsafeImageryPath(ValueError):
    """An event imagery `source` that does not name a GeoTIFF inside `cache/gee/<site_id>/`."""


#: Event imagery GeoTIFFs live only here (repo-root-relative), one folder per site.
IMAGERY_CACHE_ROOT = ("cache", "gee")
IMAGERY_SUFFIXES = (".tif", ".tiff")


def raw_rgb_path(source: str | None, repo_root: Path = REPO_ROOT, site_id: str | None = None) -> Path:
    """The `..._rgb.tif` sibling of the analysis GeoTIFF an event's `imagery_pre_event`/
    `imagery_post_event.source` points at (e.g. `cache/gee/teesta/teesta_pre_event.tif` ->
    `cache/gee/teesta/teesta_pre_event_rgb.tif`), resolved under `repo_root`.

    `source` comes from a site config, which `POST /sites` accepts from a client, and the returned
    path is later READ (convert) and WRITTEN (live refresh). So it must be a relative POSIX path
    `cache/gee/<site>/.../<name>.tif|.tiff` with no `..`, no absolute/drive/backslash/NUL parts, and
    its resolved location (symlinks followed) must stay inside `<repo_root>/cache/gee/` -- and inside
    `cache/gee/<site_id>/` when `site_id` is given, so one site cannot read or overwrite another
    site's imagery. Anything else raises `UnsafeImageryPath` (a `ValueError`) before any I/O."""
    if not source:
        raise ValueError("imagery source is not set (SourcedValue.source is empty)")
    if not isinstance(source, str) or any(c in source for c in ("\\", "\x00", ":")):
        raise UnsafeImageryPath("imagery source must be a plain relative POSIX path under cache/gee/")
    rel = PurePosixPath(source)
    if rel.is_absolute() or ".." in rel.parts:
        raise UnsafeImageryPath("imagery source must be relative and must not contain '..'")
    root_n = len(IMAGERY_CACHE_ROOT)
    if rel.parts[:root_n] != IMAGERY_CACHE_ROOT or len(rel.parts) < root_n + 2:
        raise UnsafeImageryPath("imagery source must be a file under cache/gee/<site_id>/")
    if site_id is not None and rel.parts[root_n] != site_id:
        raise UnsafeImageryPath(f"imagery source must be under cache/gee/{site_id}/ for site '{site_id}'")
    if rel.suffix.lower() not in IMAGERY_SUFFIXES:
        raise UnsafeImageryPath(f"imagery source must be a GeoTIFF ({', '.join(IMAGERY_SUFFIXES)})")
    rgb = Path(repo_root).joinpath(*rel.with_name(f"{rel.stem}_rgb{rel.suffix}").parts)
    allowed = Path(repo_root).joinpath(*IMAGERY_CACHE_ROOT, *((site_id,) if site_id else ())).resolve()
    if not rgb.resolve().is_relative_to(allowed):
        raise UnsafeImageryPath("imagery source resolves outside cache/gee/ (symlink escape)")
    return rgb


def staging_path(final: Path) -> Path:
    """Where a live render writes a phase's RGB GeoTIFF before it is swapped in."""
    return final.with_name(final.stem + ".staging" + final.suffix)


def _fallback_shape(width: int, height: int, max_px: int = FALLBACK_MAX_PX) -> tuple[int, int]:
    if max(width, height) <= max_px:
        return height, width
    scale = max_px / max(width, height)
    return max(1, round(height * scale)), max(1, round(width * scale))


def _write_png(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with rasterio.open(tmp, "w", driver="PNG", width=arr.shape[2], height=arr.shape[1],
                        count=arr.shape[0], dtype=np.uint8) as dst:
        dst.write(arr)
    tmp.replace(path)
    sidecar = tmp.with_suffix(tmp.suffix + ".aux.xml")  # GDAL PNG driver may write one alongside
    if sidecar.is_file():
        sidecar.unlink()


def _convert_one(raw_tif: Path, stem: str, imagery_dir: Path) -> dict:
    if not raw_tif.is_file():
        raise FileNotFoundError(
            f"missing RGB GeoTIFF: expected '{raw_tif}' but it does not exist"
        )
    with rasterio.open(raw_tif) as ds:
        if ds.count < 3:
            raise ValueError(f"'{raw_tif}' has {ds.count} band(s); need at least 3 (RGB)")
        full = np.clip(ds.read([1, 2, 3]), 0, 255).astype(np.uint8)
        fh, fw = _fallback_shape(ds.width, ds.height)
        small = np.clip(
            ds.read([1, 2, 3], out_shape=(3, fh, fw), resampling=Resampling.average), 0, 255
        ).astype(np.uint8)
        west, south, east, north = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)

    png_path = imagery_dir / f"{stem}.png"
    fallback_path = imagery_dir / f"{stem}_fallback.png"
    _write_png(full, png_path)
    _write_png(small, fallback_path)

    return {
        "png": f"imagery/{png_path.name}",
        "fallback_png": f"imagery/{fallback_path.name}",
        "bounds_latlng": [[south, west], [north, east]],
        "width": int(full.shape[2]), "height": int(full.shape[1]),
    }


def _merge_gee_meta_imagery(
    site_id: str, entries: list[dict], scene_ids: dict[str, list[str]], source: str, data_dir: Path,
    cloud_pct: float | None = None, acquisition_dates: list[str] | None = None,
) -> None:
    """Merges an `"imagery"` product entry into `gee_meta.json` (contract §4.8), keeping whatever
    `fetch.py` already wrote for `lake_area`/`lake_latest`/`rainfall` untouched."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_scene_ids = sorted({sid for ids in scene_ids.values() for sid in ids})
    meta = cache.read_json(site_id, "gee_meta.json", data_dir) or {"site_id": site_id}
    meta["imagery"] = {
        "dataset": "sentinel-2", "scene_ids": all_scene_ids,
        "acquisition_dates": sorted(set(acquisition_dates or [e["date"] for e in entries])),
        "event_dates": sorted({e["date"] for e in entries}),
        "cloud_pct": cloud_pct, "fetched_at": now, "last_attempt_at": now, "source": source,
        "method": "least-cloudy Sentinel-2 RGB within +/-15 days of the event date" if source == "live"
                  else "operator-staged RGB GeoTIFF",
        "error": None, "fallback_reason": None,
    }
    cache.write_json(site_id, "gee_meta.json", meta, data_dir)


def _record_imagery_failure(site_id: str, error: str, data_dir: Path, reason: str) -> None:
    """A failed live attempt keeps the previous imagery provenance (incl. its `fetched_at`) and only
    records the attempt, the sanitised error and why the cached copy is served."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = cache.read_json(site_id, "gee_meta.json", data_dir) or {"site_id": site_id}
    entry = dict(meta.get("imagery") or {"dataset": "sentinel-2", "scene_ids": [], "acquisition_dates": [],
                                           "cloud_pct": None, "fetched_at": None})
    entry.update(last_attempt_at=now, error=error, fallback_reason=reason)
    entry["source"] = "cache" if (entry.get("source") in ("live", "cache")) else "none"
    meta["imagery"] = entry
    cache.write_json(site_id, "gee_meta.json", meta, data_dir)


def convert(
    site_id: str, cfg: SiteConfig | None = None, data_dir: str | Path = cache.DATA_DIR,
    repo_root: str | Path = REPO_ROOT, scene_ids: dict[str, list[str]] | None = None,
    source: str = "cache",
) -> Path:
    """Converts the site's staged pre-/post-event RGB GeoTIFFs into `data/<site_id>/gee/imagery/`
    PNGs + a `manifest.json`; returns the manifest's path. Raises `FileNotFoundError` naming the
    exact missing GeoTIFF, or `ValueError` if no event has imagery dates set -- never silently
    skips a phase.

    `scene_ids`, if given, is `{"pre": [...], "post": [...]}` -- the satellite scene(s) the staged
    GeoTIFFs were exported from -- and gets merged into `gee_meta.json`'s `"imagery"` entry
    (`source` labels where the *imagery* itself came from: `"cache"` for an operator-staged export,
    `"live"` when called from `refresh()` after a real re-render)."""
    cfg = cfg or load_site_config(site_id)
    data_dir = Path(data_dir)
    repo_root = Path(repo_root)
    event = _event_for_imagery(cfg)
    imagery_dir = cache.gee_dir(site_id, data_dir) / "imagery"

    entries = []
    for phase in PHASES:
        sv = event.imagery_pre_event if phase == "pre" else event.imagery_post_event
        raw_tif = raw_rgb_path(sv.source, repo_root, site_id)
        date_str = str(sv.value)[:10]
        stem = f"{event.id}_{phase}_{date_str.replace('-', '')}"
        entry = _convert_one(raw_tif, stem, imagery_dir)
        entries.append({"event_id": event.id, "phase": phase, "date": date_str, **entry})

    manifest = {"site_id": site_id, "event_id": event.id, "imagery": entries}
    manifest_path = cache.write_json(site_id, MANIFEST_NAME, manifest, data_dir)

    if scene_ids:
        _merge_gee_meta_imagery(site_id, entries, scene_ids, source, data_dir)

    return manifest_path


def refresh(
    site_id: str, cfg: SiteConfig | None = None, data_dir: str | Path = cache.DATA_DIR,
    repo_root: str | Path = REPO_ROOT, ee_project: str | None = None,
) -> ImageryResult:
    """Tries to re-render the event's pre-/post-event RGB composites live from Earth Engine,
    overwriting the staged `_rgb.tif` files, then reconverts. Falls back to whatever is already
    on disk on any failure -- missing credentials, no network, or no usable scene near the date."""
    cfg = cfg or load_site_config(site_id)
    data_dir = Path(data_dir)
    result = ImageryResult(site_id=site_id)
    rendered = None
    staged: list[Path] = []

    try:
        from .live_render import render_event_rgb  # deferred: only needed for a live attempt

        event = _event_for_imagery(cfg)
        staged = [staging_path(raw_rgb_path(sv.source, Path(repo_root), site_id))
                  for sv in (event.imagery_pre_event, event.imagery_post_event)]
        rendered = render_event_rgb(cfg, event, repo_root=Path(repo_root), ee_project=ee_project)
        if isinstance(rendered, dict):
            missing = [ph for ph in PHASES if ph not in rendered]
            if missing:
                raise RuntimeError(f"live render did not return phase(s) {missing}")
            for info in rendered.values():  # every phase rendered: only now replace the originals
                Path(info["staged"]).replace(info["final"])
        result.source = "live"
    except Exception as e:
        err = cache.safe_error(e)
        log.warning("live imagery refresh failed for '%s', keeping cached RGB tifs: %s", site_id, err)
        result.errors.append(f"imagery: {err}")
        result.source = "cache"
        rendered = None
    finally:
        for p in staged:  # never leave a half-finished render behind
            if p.is_file():
                p.unlink()

    try:
        if result.source == "live" and isinstance(rendered, dict):
            scene_ids = {ph: [rendered[ph]["scene_id"]] for ph in PHASES if rendered[ph].get("scene_id")}
            clouds = [rendered[ph]["cloud_pct"] for ph in PHASES if rendered[ph].get("cloud_pct") is not None]
            acq = [rendered[ph]["acquisition_date"] for ph in PHASES if rendered[ph].get("acquisition_date")]
            result.manifest_path = convert(site_id, cfg=cfg, data_dir=data_dir, repo_root=repo_root)
            manifest = cache.read_json(site_id, MANIFEST_NAME, data_dir) or {}
            _merge_gee_meta_imagery(site_id, manifest.get("imagery", []), scene_ids, "live", data_dir,
                                    cloud_pct=max(clouds) if clouds else None, acquisition_dates=acq)
        else:
            result.manifest_path = convert(site_id, cfg=cfg, data_dir=data_dir, repo_root=repo_root)
            if result.errors:
                _record_imagery_failure(site_id, result.errors[-1].removeprefix("imagery: "), data_dir,
                                        "provider_error")
    except (FileNotFoundError, ValueError) as e:
        # Nothing staged on disk to fall back to either: report it, don't fabricate imagery.
        err = cache.safe_error(e)
        result.errors.append(f"imagery: {err}")
        result.source = "none" if result.source != "live" else result.source
        existing = cache.gee_dir(site_id, data_dir) / MANIFEST_NAME
        result.manifest_path = existing if existing.is_file() else None
        _record_imagery_failure(site_id, err, data_dir, "no_cached_imagery")
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path, default=cache.DATA_DIR)
    parser.add_argument("--refresh", action="store_true", help="try a live Earth Engine re-render first")
    parser.add_argument("--ee-project", default=None)
    parser.add_argument("--pre-scene-id", action="append", default=[],
                         help="scene ID the staged pre-event GeoTIFF was exported from (repeatable)")
    parser.add_argument("--post-scene-id", action="append", default=[],
                         help="scene ID the staged post-event GeoTIFF was exported from (repeatable)")
    args = parser.parse_args(argv)
    scene_ids = {"pre": args.pre_scene_id, "post": args.post_scene_id}
    scene_ids = scene_ids if (scene_ids["pre"] or scene_ids["post"]) else None

    try:
        if args.refresh:
            result = refresh(args.site_id, data_dir=args.data_dir, ee_project=args.ee_project)
            print(f"source: {result.source}")
            for e in result.errors:
                log.warning("%s", e)
            print(f"wrote {result.manifest_path}")
        else:
            manifest_path = convert(args.site_id, data_dir=args.data_dir, scene_ids=scene_ids)
            print(f"wrote {manifest_path}")
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

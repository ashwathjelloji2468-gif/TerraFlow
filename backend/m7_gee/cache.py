"""`data/<site_id>/gee/` cache helpers (contract §4.8): atomic writes, merging new `lake_area.csv`
rows with what is already on disk (so a re-run doesn't refetch finished past months), and reading
everything back as a `GeeLayers` payload (contract §5.8) for M0 to serve.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

CONTRACT_VERSION = "0.3.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

LAKE_AREA_FIELDS = ["date", "area_m2", "method", "cloud_pct", "scene_ids"]
RAINFALL_FIELDS = ["date", "precip_mm", "dataset", "aggregation"]


PRODUCTS = ("lake_area", "lake_latest", "rainfall", "imagery")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"]+){2,}")
_SECRETY_RE = re.compile(r"(?i)(private_key|token|secret|password|key_id)\S*")


def safe_error(exc: BaseException | str, limit: int = 200) -> str:
    """A short, client-safe description of a failure: exception class + message with filesystem
    paths and anything credential-shaped redacted (CLAUDE.md rule 12 -- refresh errors are returned
    through the API and stored in `gee_meta.json`, so they must never carry a key path, key
    contents or a token)."""
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    text = _PATH_RE.sub("<path>", text)
    text = _SECRETY_RE.sub("<redacted>", text)
    text = " ".join(text.split())
    return text[:limit]


def gee_dir(site_id: str, data_dir: str | Path = DATA_DIR) -> Path:
    return Path(data_dir) / site_id / "gee"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    lines = [",".join(fields)]
    for row in rows:
        lines.append(",".join("" if row.get(f) is None else str(row[f]) for f in fields))
    _atomic_write_text(path, "\n".join(lines) + "\n")


def _read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_lake_area(site_id: str, data_dir: str | Path = DATA_DIR) -> list[dict]:
    rows = _read_csv(gee_dir(site_id, data_dir) / "lake_area.csv")
    for r in rows:
        r["area_m2"] = float(r["area_m2"]) if r.get("area_m2") not in (None, "") else None
        r["cloud_pct"] = float(r["cloud_pct"]) if r.get("cloud_pct") not in (None, "") else None
        r["method"] = r["method"] or None
    return rows


def merge_lake_rows(cached: list[dict], fresh: list[dict]) -> list[dict]:
    """Fresh rows replace cached rows for the same `date`; everything else in the cache is kept.
    A caller only needs to fetch the months it doesn't already have plus the current/latest month
    (which can still change), and this still produces the full series."""
    by_date = {r["date"]: r for r in cached}
    for r in fresh:
        by_date[r["date"]] = r
    return sorted(by_date.values(), key=lambda r: r["date"])


def write_lake_area(site_id: str, rows: list[dict], data_dir: str | Path = DATA_DIR) -> Path:
    path = gee_dir(site_id, data_dir) / "lake_area.csv"
    _write_csv(path, rows, LAKE_AREA_FIELDS)
    return path


def merge_rainfall_rows(cached: list[dict], fresh: list[dict]) -> list[dict]:
    """Same rule as `merge_lake_rows`: a live refetch replaces only the days it returned, so older
    cached days are not lost when the trailing window moves forward."""
    by_date = {r["date"]: r for r in cached}
    for r in fresh:
        by_date[r["date"]] = r
    return sorted(by_date.values(), key=lambda r: r["date"])


def write_rainfall(site_id: str, rows: list[dict], data_dir: str | Path = DATA_DIR) -> Path:
    path = gee_dir(site_id, data_dir) / "rainfall.csv"
    _write_csv(path, rows, RAINFALL_FIELDS)
    return path


def read_rainfall(site_id: str, data_dir: str | Path = DATA_DIR) -> list[dict]:
    rows = _read_csv(gee_dir(site_id, data_dir) / "rainfall.csv")
    for r in rows:
        r["precip_mm"] = float(r["precip_mm"]) if r.get("precip_mm") not in (None, "") else None
    return rows


def write_lake_latest(site_id: str, feature: dict, data_dir: str | Path = DATA_DIR) -> Path:
    path = gee_dir(site_id, data_dir) / "lake_latest.geojson"
    fc = {"type": "FeatureCollection", "features": [feature] if feature else []}
    _atomic_write_text(path, json.dumps(fc, indent=2) + "\n")
    return path


def read_lake_latest(site_id: str, data_dir: str | Path = DATA_DIR) -> dict:
    path = gee_dir(site_id, data_dir) / "lake_latest.geojson"
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(site_id: str, name: str, payload: dict, data_dir: str | Path = DATA_DIR) -> Path:
    path = gee_dir(site_id, data_dir) / name
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def read_json(site_id: str, name: str, data_dir: str | Path = DATA_DIR) -> dict | None:
    path = gee_dir(site_id, data_dir) / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def fallback_screenshots(site_id: str, data_dir: str | Path = DATA_DIR) -> list[str]:
    fallback_dir = gee_dir(site_id, data_dir) / "fallback"
    if not fallback_dir.is_dir():
        return []
    return sorted(str(p) for p in fallback_dir.glob("*.png"))


def read_imagery(site_id: str, data_dir: str | Path = DATA_DIR) -> list[dict]:
    """`GeeLayers.imagery` entries (contract §5.8) built from `imagery.py`'s
    `gee/imagery/manifest.json`, or `[]` if imagery hasn't been converted for this site yet."""
    manifest = read_json(site_id, "imagery/manifest.json", data_dir)
    if not manifest:
        return []
    return [
        {
            "event_id": e["event_id"], "phase": e["phase"], "date": e["date"],
            "url": f"/api/v1/files/{site_id}/gee/{e['png']}",
            "fallback_url": f"/api/v1/files/{site_id}/gee/{e['fallback_png']}",
            "bounds_latlng": e["bounds_latlng"],
        }
        for e in manifest.get("imagery", [])
    ]


def observed_dir(site_id: str, data_dir: str | Path = DATA_DIR) -> Path:
    """The canonical observed-extent directory (docs/handoff_contract.md §1.8, updated
    2026-09-28): `data/<site_id>/observed/`. Not under `gee/` -- a digitized observed extent is
    operator-supplied, not an M7 Earth Engine fetch product, even though `observed.convert`
    (the tool that produces it) lives in this module."""
    return Path(data_dir) / site_id / "observed"


def observed_extent_path(site_id: str, onset_year: str, data_dir: str | Path = DATA_DIR) -> Path:
    """The one canonical observed-extent file for an event whose onset falls in `onset_year`:
    `data/<site_id>/observed/flood_extent_<onset_year>.geojson` (EPSG:4326)."""
    return observed_dir(site_id, data_dir) / f"flood_extent_{onset_year}.geojson"


def write_observed_extent(site_id: str, onset_year: str, payload: dict, data_dir: str | Path = DATA_DIR) -> Path:
    path = observed_extent_path(site_id, onset_year, data_dir)
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def read_observed_extents(site_id: str, data_dir: str | Path = DATA_DIR) -> list[dict]:
    """`GeeLayers.observed_extents` entries (contract §5.8) built from every
    `observed/flood_extent_<year>.geojson` file (`observed.convert`), or `[]` if none has been
    digitized for this site yet. `event_id`/`method` come from the properties `observed.convert`
    already stamped onto every feature, not from the (year-based) filename."""
    obs_dir = observed_dir(site_id, data_dir)
    if not obs_dir.is_dir():
        return []
    out = []
    for path in sorted(obs_dir.glob("flood_extent_*.geojson")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features", [])
        if not features:
            continue
        props = features[0].get("properties", {})
        out.append({
            "event_id": props.get("event_id"),
            "url": f"/api/v1/files/{site_id}/observed/{path.name}",
            "method": props.get("method"),
        })
    return out


def _product_status(meta: dict, name: str) -> dict:
    """`GeeLayers.products[name]` (additive, optional field): where the data now on disk for this
    product came from, as recorded by the last fetch in `gee_meta.json`. `fetched_at` is the last
    time Earth Engine actually returned this product (never stamped by a cache-only run)."""
    m = meta.get(name) or {}
    return {
        "source": m.get("source") if m else None,
        "dataset": m.get("dataset"),
        "fetched_at": m.get("fetched_at"),
        "last_attempt_at": m.get("last_attempt_at"),
        "acquisition_dates": m.get("acquisition_dates", []),
        "scene_ids": m.get("scene_ids", []),
        "cloud_pct": m.get("cloud_pct"),
        "method": m.get("method"),
        "error": m.get("error"),
        "fallback_reason": m.get("fallback_reason"),
    }


def load_layers(site_id: str, data_dir: str | Path = DATA_DIR) -> dict:
    """Everything on disk assembled into a `GeeLayers` payload (contract §5.8), for M0 to serve
    from `GET /gee/{site_id}`.

    Top-level `source` describes what is being served *now*, which for a GET is always the on-disk
    cache: `cache` if any real product (lake area, rainfall, lake polygon or imagery) is on disk,
    `screenshot_fallback` if none is but `fallback/*.png` screenshots exist (contract §4.8). It is
    never `live` here -- only `POST /gee/{id}/refresh` can say `live`, and only for products Earth
    Engine actually returned in that request (`backend/m0_api/main.py`). Per-product provenance
    (incl. which products last came from a live fetch, and when) is in the additive `products`
    field, read from `gee_meta.json`; `partial` flags a last fetch where some products failed.
    """
    from .settings import GeeSettings

    meta = read_json(site_id, "gee_meta.json", data_dir) or {}
    lake_area_rows = read_lake_area(site_id, data_dir)
    rainfall_rows = read_rainfall(site_id, data_dir)
    lake_latest = read_lake_latest(site_id, data_dir)
    imagery = read_imagery(site_id, data_dir)
    recheck = read_json(site_id, "recheck.json", data_dir) or {}
    threshold = recheck.get("threshold_pct")
    if threshold is None:
        threshold = GeeSettings().recheck_threshold_pct

    has_data = bool(lake_area_rows or rainfall_rows or lake_latest.get("features") or imagery)
    screenshots = fallback_screenshots(site_id, data_dir)
    source = "cache" if has_data else ("screenshot_fallback" if screenshots else "cache")

    products = {name: _product_status(meta, name) for name in PRODUCTS}
    partial = any(p["error"] for p in products.values()) and any(
        p["source"] == "live" for p in products.values())

    return {
        "site_id": site_id,
        "source": source,
        "fetched_at": meta.get("fetched_at") or "",
        "lake_area_series": [
            {"date": r["date"], "area_m2": r["area_m2"], "method": r["method"], "cloud_pct": r["cloud_pct"]}
            for r in lake_area_rows
        ],
        "lake_latest": lake_latest,
        "rainfall": [
            {"date": r["date"], "precip_mm": r["precip_mm"], "dataset": r["dataset"]} for r in rainfall_rows
        ],
        "imagery": imagery,
        "observed_extents": read_observed_extents(site_id, data_dir),
        "recheck": {"outdated": bool(recheck.get("outdated", False)), "change_pct": recheck.get("change_pct"),
                     "threshold_pct": threshold, "reason": recheck.get("reason"),
                     "checked_at": recheck.get("checked_at"),
                     "reference_area_m2": recheck.get("reference_area_m2"),
                     "latest_area_m2": recheck.get("latest_area_m2")},
        # --- additive, optional fields (contract §5.8 top level allows extra properties) ---
        "data_available": has_data,
        "fallback_screenshot_count": len(screenshots),
        "products": products,
        "partial": bool(partial),
        "rainfall_accumulations": meta.get("rainfall_accumulations"),
        "rainfall_kind": "satellite_estimate",
        "last_refresh": meta.get("last_refresh"),
    }

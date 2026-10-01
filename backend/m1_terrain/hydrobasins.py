"""Feature 2: HydroBASINS upstream catchment for a site -> `data/<site_id>/raw/hydrobasins_catchment.geojson`.

The catchment is the HydroBASINS sub-basin containing the most-upstream dam's `location` plus every
sub-basin upstream of it (walked through `NEXT_DOWN`, the same pure helper M7 uses:
`backend.m7_gee.provider.walk_upstream_basin_ids`). Source (docs/decisions.md 2026-10-01, D3):

1. **Primary: local shapefiles** in `$HYDROBASINS_DIR` -- the HydroSHEDS standard product
   `hybas_<region>_lev<NN>_v1c.shp` (region af, ar, as, au, eu, gr, na, sa, si), downloaded once
   from https://www.hydrosheds.org/products/hydrobasins (no API key; HydroSHEDS licence).
2. **Secondary: Earth Engine** `WWF/HydroSHEDS/v1/Basins/hybas_<level>` via M7's provider, only
   when Earth Engine credentials are configured (`GEE_PROJECT` or a service account).

Optional (D5): when neither source is usable the provenance entry is `status: "unavailable"` with
the reason -- never `fetched`, and no file is written. `SUB_AREA`/`UP_AREA` keep HydroBASINS' own
km² as distributed; `upstream_area_m2` in provenance is the SI total.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from backend.shared.site_config import SiteConfig

log = logging.getLogger("m1.hydrobasins")

SOURCE_ID = "src_041"
VERSION = "v1c"
LICENSE = "HydroSHEDS licence agreement (free for scientific, educational and commercial use, with attribution); see https://www.hydrosheds.org"
CITATION = "Lehner, B., Grill, G. (2013), Hydrological Processes 27(15):2171-2186, doi:10.1002/hyp.9740"
HYDROBASINS_DIR_ENV = "HYDROBASINS_DIR"
OUTPUT_NAME = "hydrobasins_catchment.geojson"
FIELDS = ["HYBAS_ID", "NEXT_DOWN", "SUB_AREA", "UP_AREA"]


class CatchmentUnavailable(RuntimeError):
    """No usable HydroBASINS source; the message says why and what to set."""


def _seed(cfg: SiteConfig) -> tuple[float, float]:
    dam = cfg.dams[0]
    if dam.location.value is None:
        raise CatchmentUnavailable(f"dams[0] ({dam.id}).location is a placeholder (null); a seed point is required")
    lon, lat = dam.location.value
    return float(lon), float(lat)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _shapefiles(root: Path, level: int) -> list[Path]:
    pattern = f"hybas_*_lev{level:02d}_{VERSION}.shp"
    return sorted({*root.rglob(pattern), *root.rglob(pattern.upper())})


def catchment_from_local(root: Path, lon: float, lat: float, level: int) -> tuple[dict, dict]:
    """(FeatureCollection, provenance details) from local HydroBASINS shapefiles."""
    import geopandas as gpd
    from shapely.geometry import Point

    from backend.m7_gee.provider import walk_upstream_basin_ids

    files = _shapefiles(root, level)
    if not files:
        raise CatchmentUnavailable(
            f"{HYDROBASINS_DIR_ENV}={root} contains no hybas_*_lev{level:02d}_{VERSION}.shp; download the "
            "level-%02d shapefile for your region from https://www.hydrosheds.org/products/hydrobasins" % level)
    point = Point(lon, lat)
    for shp in files:
        seed = gpd.read_file(shp, bbox=(lon, lat, lon, lat))
        seed = seed[seed.geometry.contains(point)] if len(seed) else seed
        if not len(seed):
            continue
        table = gpd.read_file(shp, columns=["HYBAS_ID", "NEXT_DOWN"], ignore_geometry=True)
        upstream: dict[int, list[int]] = {}
        for hid, nxt in zip(table["HYBAS_ID"].astype("int64"), table["NEXT_DOWN"].astype("int64")):
            upstream.setdefault(int(nxt), []).append(int(hid))
        seed_id = int(seed.iloc[0]["HYBAS_ID"])
        ids = walk_upstream_basin_ids(seed_id, lambda frontier: [u for f in frontier for u in upstream.get(int(f), [])])
        basins = gpd.read_file(shp, where=f"HYBAS_ID IN ({','.join(str(i) for i in ids)})")
        basins = basins[[c for c in FIELDS if c in basins.columns] + ["geometry"]].to_crs(4326)
        fc = json.loads(basins.to_json())
        details = {
            "method": "local_shapefile", "files": [shp.name], "sha256": _sha256(shp),
            "seed_basin_id": seed_id, "n_basins": len(basins),
            "upstream_area_m2": float(basins["SUB_AREA"].sum()) * 1e6 if "SUB_AREA" in basins else None,
        }
        return fc, details
    raise CatchmentUnavailable(f"the seed point ({lon}, {lat}) is in none of {[f.name for f in files]} -- wrong HydroBASINS region?")


def _earth_engine_configured() -> bool:
    from backend.m7_gee.scene_search import GEE_PROJECT_ENV, _env_value, gee_service_account_credentials

    return bool(_env_value(GEE_PROJECT_ENV)) or gee_service_account_credentials() is not None


def catchment_from_earth_engine(lon: float, lat: float, level: int) -> tuple[dict, dict]:
    from backend.m7_gee.provider import EarthEngineProvider
    from backend.m7_gee.scene_search import _ee_initialize

    _ee_initialize(None)
    result = EarthEngineProvider().catchment(lon, lat, level)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": result["geojson"], "properties": {"basin_ids": result["basin_ids"]}}]}
    return fc, {"method": "earth_engine", "collection": f"WWF/HydroSHEDS/v1/Basins/hybas_{level}",
                "n_basins": len(result["basin_ids"]), "upstream_area_m2": None}


def fetch_catchment(cfg: SiteConfig, raw_dir: Path, *, level: int = 12, network: bool = True) -> dict:
    """Write the catchment and return its provenance entry (`status` fetched | unavailable)."""
    raw_dir = Path(raw_dir)
    base = {"dataset": f"hydrobasins_lev{level:02d}_{VERSION}", "source": SOURCE_ID, "level": level,
            "license": LICENSE, "citation": CITATION, "fetched_at": datetime.now(timezone.utc).isoformat()}
    reasons = []
    try:
        lon, lat = _seed(cfg)
    except CatchmentUnavailable as e:
        return {**base, "status": "unavailable", "reason": str(e)}
    base.update({"seed_lonlat": [lon, lat], "seed_basis": f"dams[0] ({cfg.dams[0].id}).location"})
    attempts = []
    root = os.environ.get(HYDROBASINS_DIR_ENV)
    if root:
        attempts.append(("local", lambda: catchment_from_local(Path(root), lon, lat, level)))
    else:
        reasons.append(f"{HYDROBASINS_DIR_ENV} is not set")
    if network and _earth_engine_configured():
        attempts.append(("earth_engine", lambda: catchment_from_earth_engine(lon, lat, level)))
    elif not network:
        reasons.append("Earth Engine not tried: network ingestion disabled (SIH26_INGEST_NETWORK=off)")
    else:
        reasons.append("Earth Engine credentials are not configured (GEE_PROJECT or a service account)")
    for name, attempt in attempts:
        try:
            fc, details = attempt()
        except Exception as e:  # noqa: BLE001 -- recorded verbatim as the unavailable reason
            reasons.append(f"{name}: {e}")
            continue
        out = raw_dir / OUTPUT_NAME
        raw_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fc) + "\n", encoding="utf-8")
        return {**base, **details, "status": "fetched", "file": OUTPUT_NAME, "crs": "EPSG:4326",
                "area_units": "SUB_AREA/UP_AREA km^2 (as distributed); upstream_area_m2 SI"}
    return {**base, "status": "unavailable", "reason": "; ".join(reasons)}

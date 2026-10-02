"""M1-2/3: the terrain pipeline — `data/<site_id>/raw/` (M1-1) -> `data/<site_id>/terrain/`
(`docs/handoff_contract.md` §4.1).

Order: canonical grids -> resample + fill voids (far- and near-field DEM, landcover) -> lake/
reservoir extent (`water.py`) -> a first flow-routing pass on the raw DEM, used only to orient
each dam's crest against the local flow direction -> burn dams and reservoirs into the DEM
(`burn.py`) -> a second flow-routing pass on the burned DEM, with lake cells masked out (not
sink-filled — a moraine lake's pit is a real feature, not a DEM error) -> centreline + chainage +
POIs (`centreline.py`) -> HAND -> the valley-corridor domain (`domain.py`) -> roughness
(`roughness.py`) -> near-field STL (`stl.py`) -> `provenance.json`.

CLI: `python -m backend.m1_terrain.pipeline <site_id> --dem <product> [--domain-max-hand-m 50]
[--water-polygon PATH] [--data-dir DIR]`. `--dem` has no default: the DEM comparison report
(M1-2, still to come) is what should pick the product, this module just takes the answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

from backend.shared.grid import CanonicalGrid, build_site_grids, write_grid_raster
from backend.shared.site_config import SiteConfig, default_data_dir, load_site_config

from . import burn, centreline, dem as dem_mod, domain as domain_mod, hydro, roughness, stl, water
from .settings import TerrainSettings

log = logging.getLogger("m1.pipeline")

CONTRACT_VERSION = "0.3.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
MANNING_TABLE_PATH = REPO_ROOT / "config" / "manning_n.csv"

DEM_PRODUCTS = ["srtm_gl1", "copernicus_glo30", "cartodem"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_version() -> str:
    """Delegates to `backend.shared.version` (Feature 12)."""
    from backend.shared.version import code_version
    return code_version()

def _write_line_gpkg(path: Path, x_m: np.ndarray, y_m: np.ndarray, grid: CanonicalGrid, total_chainage_m: float) -> None:
    line = LineString(zip(x_m, y_m))
    gdf = gpd.GeoDataFrame(
        {"site_id": [grid.site_id], "total_chainage_m": [total_chainage_m]},
        geometry=[line], crs=grid.crs.to_epsg(),
    )
    gdf.to_file(path, driver="GPKG")


def _write_pois_gpkg(path: Path, pois_df: pd.DataFrame, grid: CanonicalGrid) -> None:
    x = grid.origin_x + (pois_df["col"].to_numpy() + 0.5) * grid.cell_size_m
    y = grid.origin_y - (pois_df["row"].to_numpy() + 0.5) * grid.cell_size_m
    gdf = gpd.GeoDataFrame(
        pois_df, geometry=[Point(xi, yi) for xi, yi in zip(x, y)], crs=grid.crs.to_epsg(),
    )
    gdf.to_file(path, driver="GPKG")


def build_terrain(
    cfg: SiteConfig,
    dem_product: str,
    raw_dir: str | Path,
    out_dir: str | Path,
    settings: TerrainSettings | None = None,
    water_polygon_path: str | Path | None = None,
) -> dict:
    """Run the full pipeline for `cfg`, reading `raw_dir` (M1-1's downloads) and writing every
    contract §4.1 file to `out_dir`. Returns the `provenance.json` dict that was written."""
    settings = settings or TerrainSettings()
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    site_id = cfg.site.id
    steps: list[dict] = []

    raw_provenance = {}
    raw_prov_path = raw_dir / "provenance.json"
    if raw_prov_path.is_file():
        raw_provenance = json.loads(raw_prov_path.read_text(encoding="utf-8"))
    dem_key = f"dem_{dem_product}"
    if dem_key not in raw_provenance:
        raise FileNotFoundError(f"{raw_prov_path}: no entry for '{dem_key}' — run backend.m1_terrain.download first")
    dem_entry = raw_provenance[dem_key]
    landcover_entry = raw_provenance.get("landcover_esa_worldcover")
    if landcover_entry is None:
        raise FileNotFoundError(f"{raw_prov_path}: no entry for 'landcover_esa_worldcover' — run backend.m1_terrain.download first")

    # 1. canonical grids
    grids = build_site_grids(cfg)
    farfield, nearfield = grids["farfield"], grids["nearfield"]
    farfield.to_json(out_dir / "grid.json")
    nearfield.to_json(out_dir / "grid_nearfield.json")
    steps.append({"stage": "grids", "farfield_shape": farfield.shape, "nearfield_shape": nearfield.shape})

    # 2. DEM: resample + fill voids, far and near
    raw_dem_path = raw_dir / dem_entry["file"]
    dem_far_raw = dem_mod.load_dem(raw_dem_path, farfield)
    dem_far, far_void_stats = dem_mod.fill_voids(dem_far_raw)
    dem_near_raw = dem_mod.load_dem(raw_dem_path, nearfield)
    dem_near, near_void_stats = dem_mod.fill_voids(dem_near_raw)
    steps.append({"stage": "dem_load_fill", "farfield": far_void_stats.to_dict(), "nearfield": near_void_stats.to_dict()})

    # 3. landcover
    raw_landcover_path = raw_dir / landcover_entry["file"]
    landcover = dem_mod.load_landcover(raw_landcover_path, farfield)
    steps.append({"stage": "landcover_load", "codes_present": sorted(int(c) for c in np.unique(landcover))})

    # 4. lake/reservoir extent
    polygon_mask = None
    if water_polygon_path is not None:
        import rasterio.features
        gdf = gpd.read_file(water_polygon_path).to_crs(farfield.crs_epsg)
        polygon_mask = rasterio.features.rasterize(
            [(geom, 1) for geom in gdf.geometry], out_shape=farfield.shape, transform=farfield.transform, fill=0,
        ).astype(bool)
    water_mask_arr, water_info = water.water_mask(landcover, cfg, farfield, settings, polygon_mask=polygon_mask)
    steps.append({"stage": "water_mask", **water_info})

    # 5. routing pass 1 (raw filled DEM) -- orient dam crests only
    network_pre_burn = hydro.route(dem_far, epsilon=settings.fill_epsilon_m)

    # 6. burn dams + reservoirs
    burn_info = []
    dem_burned = dem_far
    for dam in cfg.dams:
        dem_burned, info = burn.burn_dam_crest(dem_burned, dam, network_pre_burn, farfield, settings)
        burn_info.append(info)
    dem_burned, reservoir_info = burn.burn_reservoir(dem_burned, water_mask_arr, water.RESERVOIR)
    steps.append({"stage": "burn_in", "dams": burn_info, "reservoir": reservoir_info})

    # 7. routing pass 2 on the burned DEM, lake cells masked out of routing (not sink-filled)
    dem_for_routing = dem_burned.copy()
    dem_for_routing[water_mask_arr == water.LAKE] = -9999.0
    network = hydro.route(dem_for_routing, epsilon=settings.fill_epsilon_m)

    # 8. centreline + chainage + POIs, from the most-upstream dam's breach location
    upstream_dam = cfg.dams[0]
    breach_cell = centreline.find_breach_cell(upstream_dam, network, farfield, settings)
    if breach_cell is None:
        raise ValueError(
            f"{upstream_dam.id}: breach_location is a placeholder or outside the grid — "
            "cannot build the centreline without it"
        )
    path = centreline.trace_centreline(network, *breach_cell, farfield)
    chainage_samples = centreline.resample_chainage(path, dem_burned, farfield.cell_size_m)
    channel_mask = centreline.channel_mask_from_path(path, farfield)
    pois_df, skipped_pois = centreline.snap_pois(site_id, cfg.points_of_interest, path, farfield)
    steps.append({
        "stage": "centreline", "breach_row": breach_cell[0], "breach_col": breach_cell[1],
        "n_cells": len(path), "total_chainage_m": float(path["chainage_m"].iloc[-1]),
        "pois_snapped": len(pois_df), "pois_skipped": skipped_pois,
    })

    # 9. HAND
    hand_arr = hydro.hand(network, channel_mask)
    steps.append({"stage": "hand"})

    # 10. domain
    domain_mask_arr = domain_mod.domain_mask(hand_arr, channel_mask, settings.domain_max_hand_m)
    domain_gdf = domain_mod.domain_polygon(domain_mask_arr, farfield)
    steps.append({
        "stage": "domain", "max_hand_m": settings.domain_max_hand_m,
        "domain_cells": int(domain_mask_arr.sum()), "total_cells": int(domain_mask_arr.size),
    })

    # 11. roughness
    manning_table = roughness.load_manning_table(MANNING_TABLE_PATH)
    roughness_arr, roughness_info = roughness.roughness(
        landcover, manning_table, channel_mask=channel_mask, channel_class_code=settings.channel_class_code,
    )
    steps.append({"stage": "roughness", **roughness_info, "table_path": str(MANNING_TABLE_PATH), "table_sha256": _sha256(MANNING_TABLE_PATH)})

    # 12. near-field STL
    frame = stl.nearfield_frame(nearfield)
    triangles = stl.build_triangles(dem_near, nearfield, frame)
    steps.append({"stage": "nearfield_stl", "n_triangles": len(triangles)})

    # --- write everything ---
    write_grid_raster(out_dir / "dem.tif", dem_burned, farfield)
    write_grid_raster(out_dir / "dem_nearfield.tif", dem_near, nearfield)
    write_grid_raster(out_dir / "landcover.tif", landcover, farfield)
    write_grid_raster(out_dir / "roughness.tif", roughness_arr, farfield)
    write_grid_raster(out_dir / "hand.tif", hand_arr, farfield)
    write_grid_raster(out_dir / "domain_mask.tif", domain_mask_arr, farfield)
    write_grid_raster(out_dir / "water_mask.tif", water_mask_arr, farfield)
    domain_gdf.to_file(out_dir / "domain.gpkg", driver="GPKG")
    _write_line_gpkg(out_dir / "centreline.gpkg", path["x_m"].to_numpy(), path["y_m"].to_numpy(), farfield, float(path["chainage_m"].iloc[-1]))
    chainage_samples.to_csv(out_dir / "chainage_samples.csv", index=False)
    if len(pois_df):
        _write_pois_gpkg(out_dir / "pois.gpkg", pois_df, farfield)
    stl.write_nearfield_frame(nearfield, out_dir / "nearfield_frame.json")
    stl.write_stl(triangles, out_dir / "nearfield.stl")

    placeholder_fields = sorted(set(cfg.placeholder_fields))
    manning_placeholders = [f"manning_n.csv[worldcover_code={c}]" for c in roughness_info["placeholder_codes"]]
    has_placeholders = bool(placeholder_fields) or roughness_info["has_placeholders"]

    provenance = {
        "contract_version": CONTRACT_VERSION, "site_id": site_id, "code_version": _code_version(),
        "has_placeholders": has_placeholders,
        "placeholder_fields": placeholder_fields + manning_placeholders,
        "dem": {"product": dem_product, **dem_entry},
        # Feature 2: the DEM comparison + deterministic selection, when ingest.py made it.
        "dem_selection": (json.loads((raw_dir / "dem_comparison.json").read_text(encoding="utf-8"))
                          if (raw_dir / "dem_comparison.json").is_file() else None),
        "landcover": landcover_entry,
        "vertical_datum": dem_entry.get("vertical_datum"),
        "landcover_legend": dem_mod.landcover_legend(),
        "settings": settings.model_dump(),
        "steps": steps,
        "caveats": [
            {"id": "dem_resolution", "severity": "info", "text_key": "caveat_dem_resolution"},
            {"id": "placeholder_data", "severity": "warning" if has_placeholders else "info", "text_key": "caveat_placeholder_data"},
        ] + ([{"id": "placeholder_data", "severity": "warning", "text_key": "caveat_reservoir_bathymetry_unknown"}]
             if reservoir_info.get("burned") else []),
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    return provenance


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_id", help="a registered site id (bundled sites/<id>.yaml or onboarded data/<id>/config/<id>.yaml)")
    parser.add_argument("--dem", required=True, choices=DEM_PRODUCTS, help="which downloaded DEM product to use")
    parser.add_argument("--domain-max-hand-m", type=float, default=None, help="override TerrainSettings.domain_max_hand_m")
    parser.add_argument("--water-polygon", default=None, help="optional vector file overriding the WorldCover-derived lake/reservoir extent")
    parser.add_argument("--data-dir", default=None, help="override the data/ root (default: $SIH26_DATA_DIR, else <repo>/data)")
    args = parser.parse_args(argv)

    cfg = load_site_config(args.site_id)
    settings_kwargs = {}
    if args.domain_max_hand_m is not None:
        settings_kwargs["domain_max_hand_m"] = args.domain_max_hand_m
    settings = TerrainSettings(**settings_kwargs)

    data_dir = Path(args.data_dir or default_data_dir())
    raw_dir = data_dir / cfg.site.id / "raw"
    out_dir = data_dir / cfg.site.id / "terrain"
    build_terrain(cfg, args.dem, raw_dir, out_dir, settings, water_polygon_path=args.water_polygon)
    log.info("terrain written to %s", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

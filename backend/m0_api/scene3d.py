"""Build compact Three.js scene assets for M0's Scene3D endpoint (contract §5.9)."""

from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

from backend.m0_api import registry

# Reserve 50 kB for the JSON response so metadata + binaries stay below 20 MB decimal.
MAX_PAYLOAD_BYTES = 19_950_000
TARGET_GRID_BYTES = 12_000_000
MAX_SPH_SNAPSHOTS = 128
DEFAULT_VERTICAL_EXAGGERATION = 1.5


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_scene_inputs(query_id: str) -> tuple[str, Path, Path] | None:
    """Return (site_id, query_dir, terrain_dir) when the query median depth exists."""
    root = registry.data_dir()
    site_ids = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for site_id in site_ids:
        query_dir = registry.data_dir() / site_id / "queries" / query_id
        terrain_dir = registry.data_dir() / site_id / "terrain"
        if (query_dir / "layers" / "depth_p50.tif").is_file() and (terrain_dir / "dem.tif").is_file():
            return site_id, query_dir, terrain_dir
    return None


def _read_query_run_ids(site_id: str, query_id: str, query_dir: Path) -> list[str]:
    candidates = [query_dir / "result.json"]
    conn = None
    try:
        conn = registry.connect()
        row = conn.execute("SELECT result_path FROM queries WHERE query_id=? AND site_id=?", (query_id, site_id)).fetchone()
        if row and row["result_path"]:
            candidates.insert(0, Path(row["result_path"]))
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    for path in candidates:
        if path.is_file():
            try:
                payload = _load_json(path)
                run_ids = payload.get("provenance", {}).get("run_ids", [])
                safe_ids = [rid for rid in run_ids if isinstance(rid, str) and len(rid) <= 100 and re.fullmatch(r"[A-Za-z0-9_]+__(delft3d|sph)", rid)]
                return list(dict.fromkeys(safe_ids))
            except (OSError, ValueError, TypeError):
                continue
    return []


def _resampled_arrays(dem_path: Path, depth_path: Path, target_bytes: int) -> tuple[np.ndarray, np.ndarray, dict]:
    with rasterio.open(dem_path) as dem_ds, rasterio.open(depth_path) as depth_ds:
        factor = max(1, math.ceil(math.sqrt(dem_ds.width * dem_ds.height * 8 / target_bytes)))
        width, height = math.ceil(dem_ds.width / factor), math.ceil(dem_ds.height / factor)
        terrain = dem_ds.read(1, out_shape=(height, width), resampling=Resampling.average, masked=True)
        terrain = np.asarray(terrain.astype(np.float32).filled(-9999.0), dtype=np.float32)
        # The reduced grid preserves the DEM's complete bounds, including a partial last cell.
        transform = dem_ds.transform * Affine.scale(dem_ds.width / width, dem_ds.height / height)
        depth = np.full((height, width), -9999.0, dtype=np.float32)
        reproject(
            source=rasterio.band(depth_ds, 1), destination=depth,
            src_transform=depth_ds.transform, src_crs=depth_ds.crs,
            src_nodata=depth_ds.nodata if depth_ds.nodata is not None else -9999.0,
            dst_transform=transform, dst_crs=dem_ds.crs, dst_nodata=-9999.0,
            # Max, not average: a dry-but-in-domain cell is a real 0.0, not nodata (CLAUDE.md
            # rule 8), so averaging a narrow wet channel with its many dry neighbours would
            # dilute or erase it at any real downsample factor. Max preserves the channel's
            # peak water-surface elevation in every destination cell it touches.
            resampling=Resampling.max,
        )
        terrain_nodata = dem_ds.nodata if dem_ds.nodata is not None else -9999.0
        terrain[~np.isfinite(terrain) | (terrain == terrain_nodata)] = -9999.0
        depth[~np.isfinite(depth)] = -9999.0
        # Flood surface is a water-surface elevation; cells dry in the median stay nodata.
        flood = np.where((terrain != -9999.0) & (depth > 0), terrain + depth, -9999.0).astype(np.float32)
        valid = terrain[terrain != -9999.0]
        metadata = {
            "width": width, "height": height,
            "cell_size_x_m": abs(transform.a), "cell_size_y_m": abs(transform.e),
            "origin_x_utm_m": float(transform.c), "origin_y_utm_m": float(transform.f),
            "transform": list(transform)[:6], "crs_epsg": dem_ds.crs.to_epsg() if dem_ds.crs else None,
            "min_elev_m": float(valid.min()) if valid.size else None,
            "max_elev_m": float(valid.max()) if valid.size else None,
            "nodata": -9999.0,
        }
    return terrain, flood, metadata


def _write_bin(path: Path, array: np.ndarray) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(np.asarray(array, dtype="<f4", order="C").tobytes())
    temp.replace(path)
    return path.stat().st_size


def _write_grid_glb(path: Path, dem_path: Path, depth_path: Path, frame: dict) -> int:
    """Write a geometry-only water-surface grid in the shared local metric frame."""
    with rasterio.open(dem_path) as zds, rasterio.open(depth_path) as dds:
        height, width = zds.height, zds.width
        if width * height > 150_000:
            step = math.ceil(math.sqrt(width * height / 150_000))
            out_w, out_h = math.ceil(width / step), math.ceil(height / step)
            bed = zds.read(1, out_shape=(out_h, out_w), resampling=Resampling.average)
            depth = dds.read(1, out_shape=(out_h, out_w), resampling=Resampling.average)
            sx, sy = width / out_w, height / out_h
        else:
            bed, depth = zds.read(1), dds.read(1)
            out_h, out_w, sx, sy = height, width, 1.0, 1.0
        nodata = zds.nodata if zds.nodata is not None else -9999.0
        dnodata = dds.nodata if dds.nodata is not None else -9999.0
        valid = np.isfinite(bed) & np.isfinite(depth) & (bed != nodata) & (depth != dnodata) & (depth > 0)
        rows, cols = np.nonzero(valid)
        if len(rows) < 3:
            return 0
        x = zds.transform.c + (cols * sx + 0.5 * sx) * zds.transform.a
        y = zds.transform.f + (rows * sy + 0.5 * sy) * zds.transform.e
        xyz = np.column_stack((x - frame["origin_x"], y - frame["origin_y"], bed[rows, cols] + depth[rows, cols])).astype("<f4")
        # Keep topology only where all four neighboring cells are wet and valid.
        index = np.full((out_h, out_w), -1, dtype=np.int32)
        index[rows, cols] = np.arange(len(rows), dtype=np.int32)
        quads = []
        for r in range(out_h - 1):
            for c in range(out_w - 1):
                a, b, d, e = index[r, c], index[r, c + 1], index[r + 1, c], index[r + 1, c + 1]
                if min(a, b, d, e) >= 0:
                    quads.extend((a, d, b, b, d, e))
        indices = np.asarray(quads, dtype="<u4")
    if not len(indices):
        return 0
    # Minimal GLB writer: indexed triangles with one POSITION accessor.
    pos = xyz.tobytes()
    idx = indices.tobytes()
    pos_pad = pos + b"\0" * ((-len(pos)) % 4)
    idx_pad = idx + b"\0" * ((-len(idx)) % 4)
    gltf = {
        "asset": {"version": "2.0", "generator": "backend.m0_api.scene3d"}, "scene": 0,
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(pos_pad) + len(idx_pad)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_pad), "byteLength": len(idx), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(xyz), "type": "VEC3", "min": xyz.min(axis=0).tolist(), "max": xyz.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"},
        ],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    binary = pos_pad + idx_pad
    total = 12 + 8 + len(js) + 8 + len(binary)
    glb = struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + binary
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(glb)
    temp.replace(path)
    return path.stat().st_size


def build_scene(query_id: str, vertical_exaggeration: float) -> dict | None:
    found = find_scene_inputs(query_id)
    if found is None:
        return None
    site_id, query_dir, terrain_dir = found
    dem_path = terrain_dir / "dem.tif"
    depth_path = query_dir / "layers" / "depth_p50.tif"
    near_dem = terrain_dir / "dem_nearfield.tif"
    frame_path = terrain_dir / "nearfield_frame.json"
    grid_path = terrain_dir / "grid.json"
    if not frame_path.is_file() or not grid_path.is_file():
        return None
    frame = _load_json(frame_path)
    scene_dir = query_dir / "scene3d"
    terrain, flood, grid_meta = _resampled_arrays(dem_path, depth_path, TARGET_GRID_BYTES)
    terrain_path, flood_path = scene_dir / "terrain.bin", scene_dir / "flood_surface.bin"
    terrain_bytes = _write_bin(terrain_path, terrain)
    flood_bytes = _write_bin(flood_path, flood)

    comparison = {
        "nearfield_bounds_local": [], "delft3d_surface_url": None,
        "delft3d_surface_basis": None, "sph_surfaces": [],
    }
    comparison_bytes = 0
    run_ids = _read_query_run_ids(site_id, query_id, query_dir)
    run_dirs = [registry.data_dir() / site_id / "runs" / rid for rid in run_ids]
    for run_dir in run_dirs:
        if run_dir.name.endswith("__delft3d") and comparison["delft3d_surface_url"] is None and near_dem.is_file():
            near_depth = run_dir / "summary_nearfield" / "max_depth.tif"
            if near_depth.is_file():
                glb = scene_dir / "delft3d_surface.glb"
                nbytes = _write_grid_glb(glb, near_dem, near_depth, frame)
                if nbytes:
                    comparison["delft3d_surface_url"] = f"/api/v1/files/{site_id}/queries/{query_id}/scene3d/delft3d_surface.glb"
                    comparison["delft3d_surface_basis"] = "dem_nearfield + summary_nearfield/max_depth.tif; cellwise maximum, not a simultaneous time snapshot"
                    comparison_bytes += nbytes
        if run_dir.name.endswith("__sph"):
            for source in sorted((run_dir / "surfaces").glob("t*.glb")) if (run_dir / "surfaces").is_dir() else []:
                if len(comparison["sph_surfaces"]) >= MAX_SPH_SNAPSHOTS:
                    break
                if terrain_bytes + flood_bytes + comparison_bytes + source.stat().st_size > MAX_PAYLOAD_BYTES:
                    break
                t_s = int(source.stem[1:])
                comparison["sph_surfaces"].append({
                    "t_s": t_s,
                    "url": f"/api/v1/files/{site_id}/runs/{run_dir.name}/surfaces/{source.name}",
                })
                comparison_bytes += source.stat().st_size

    # Bound terrain/flood plus all referenced comparison assets, leaving headroom for JSON.
    if terrain_bytes + flood_bytes + comparison_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError("scene assets exceed the 20 MB payload limit")
    # Bounds are represented in local XY, lower-left then upper-right.
    origin_local_x = grid_meta["origin_x_utm_m"] - frame["origin_x"]
    origin_local_y = grid_meta["origin_y_utm_m"] - frame["origin_y"]
    near_grid_path = terrain_dir / "grid_nearfield.json"
    if near_grid_path.is_file():
        near_grid = _load_json(near_grid_path)
        near_left = near_grid["origin_x"] - frame["origin_x"]
        near_top = near_grid["origin_y"] - frame["origin_y"]
        near_width = near_grid["width"] * near_grid["cell_size_m"]
        near_height = near_grid["height"] * near_grid["cell_size_m"]
    else:
        near_left, near_top, near_width, near_height = origin_local_x, origin_local_y, 0.0, 0.0
    comparison["nearfield_bounds_local"] = [
        [near_left, near_top - near_height],
        [near_left + near_width, near_top],
    ]
    return {
        "contract_version": "0.3.0",
        "query_id": query_id,
        "frame": {
            "crs_epsg": frame["crs_epsg"], "origin_x_utm_m": frame["origin_x"], "origin_y_utm_m": frame["origin_y"],
            "vertical_exaggeration": vertical_exaggeration, "vertical_exaggeration_applies_to": "z_axis",
            "units": "m", "axis_order": "east,north,up",
        },
        "terrain": {
            "url": f"/api/v1/files/{site_id}/queries/{query_id}/scene3d/terrain.bin",
            "encoding": "float32_le_row_major", **grid_meta, "origin_local_x_m": origin_local_x,
            "origin_local_y_m": origin_local_y, "byte_length": terrain_bytes,
        },
        "flood_surface": {
            "url": f"/api/v1/files/{site_id}/queries/{query_id}/scene3d/flood_surface.bin",
            "encoding": "float32_le_row_major", "nodata": -9999.0, "basis": "terrain + depth_p50",
            "width": grid_meta["width"], "height": grid_meta["height"], "byte_length": flood_bytes,
        },
        "comparison": comparison,
        "payload_bytes": terrain_bytes + flood_bytes + comparison_bytes,
        "max_payload_mb": 20,
    }

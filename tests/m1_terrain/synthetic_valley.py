"""Synthetic V-shaped valley DEM/landcover, built from closed-form geometry over
tests/fixtures/shared/synth.yaml's bbox. NOT REAL DATA — every pipeline stage has a known-correct
answer to check against (down-valley slope, side slope, lake/reservoir bowls of a known radius and
depth, a nodata void patch placed away from the valley)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.crs import CRS

UTM_EPSG = 32645
RASTER_RES_M = 10.0  # finer than the synth site's 30 m far-field grid -> resample_to_grid really downsamples

DOWN_SLOPE = 0.03              # m drop per m downstream (valley floor gradient)
SIDE_SLOPE = 0.25              # m rise per m away from the thalweg (V-valley walls)
CREST_ELEV_AT_BREACH = 2200.0  # thalweg elevation at the breach, before any dam is burned in

LAKE_RADIUS_M = 250.0
LAKE_DEPTH_M = 20.0
RESERVOIR_RADIUS_M = 150.0
RESERVOIR_DEPTH_M = 10.0

VOID_ROW_FRAC = (0.05, 0.12)
VOID_COL_FRAC = (0.80, 0.92)

_TO_UTM = Transformer.from_crs(4326, UTM_EPSG, always_xy=True)
_TO_LONLAT = Transformer.from_crs(UTM_EPSG, 4326, always_xy=True)


def lonlat_to_utm(lon: float, lat: float) -> tuple[float, float]:
    return _TO_UTM.transform(lon, lat)


def utm_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return _TO_LONLAT.transform(x, y)


@dataclass
class ValleyGeometry:
    """An infinite line through `(origin_x, origin_y)` (the breach point, UTM) in
    `(direction_x, direction_y)` (unit vector, pointing downstream)."""

    origin_x: float
    origin_y: float
    direction_x: float
    direction_y: float

    def s_d(self, x, y):
        """`(s, d)`: signed downstream distance and unsigned cross-valley distance of `(x, y)`."""
        dx, dy = x - self.origin_x, y - self.origin_y
        s = dx * self.direction_x + dy * self.direction_y
        px, py = dx - s * self.direction_x, dy - s * self.direction_y
        return s, np.hypot(px, py)

    def point_at(self, s: float, d: float = 0.0) -> tuple[float, float]:
        """UTM `(x, y)` of the point at downstream distance `s`, cross-valley offset `d` (to the
        left of the downstream direction)."""
        px, py = -self.direction_y, self.direction_x
        return (self.origin_x + s * self.direction_x + d * px, self.origin_y + s * self.direction_y + d * py)


def valley_geometry(breach_lonlat: tuple[float, float], downstream_lonlat: tuple[float, float]) -> ValleyGeometry:
    ox, oy = lonlat_to_utm(*breach_lonlat)
    tx, ty = lonlat_to_utm(*downstream_lonlat)
    dx, dy = tx - ox, ty - oy
    norm = float(np.hypot(dx, dy))
    return ValleyGeometry(ox, oy, dx / norm, dy / norm)


def elevation(x, y, geom: ValleyGeometry, lake_center=None, reservoir_center=None, floor_half_width_m: float = 0.0):
    """`floor_half_width_m` > 0 gives the V a flat floor of that half-width (walls start at its
    edge); the default 0 is the original sharp V."""
    s, d = geom.s_d(x, y)
    elev = CREST_ELEV_AT_BREACH - DOWN_SLOPE * s + SIDE_SLOPE * np.maximum(d - floor_half_width_m, 0.0)
    for center, radius, depth in ((lake_center, LAKE_RADIUS_M, LAKE_DEPTH_M), (reservoir_center, RESERVOIR_RADIUS_M, RESERVOIR_DEPTH_M)):
        if center is None:
            continue
        r = np.hypot(x - center[0], y - center[1])
        elev = elev - depth * np.clip(1 - (r / radius) ** 2, 0, None)
    return elev


def landcover_class(x, y, geom: ValleyGeometry, lake_center=None, reservoir_center=None):
    s, d = geom.s_d(x, y)
    out = np.full(np.shape(x), 30, dtype=np.uint8)  # grassland default
    out[d > 150] = 10   # trees on the valley walls
    out[d <= 60] = 60    # bare ground / gravel bar near the channel
    for center, radius in ((lake_center, LAKE_RADIUS_M), (reservoir_center, RESERVOIR_RADIUS_M)):
        if center is None:
            continue
        out[np.hypot(x - center[0], y - center[1]) <= radius] = 80
    return out


def write_raw_rasters(
    raw_dir: Path,
    bbox_deg: tuple[float, float, float, float],
    breach_lonlat: tuple[float, float],
    downstream_lonlat: tuple[float, float],
    lake_lonlat: tuple[float, float],
    reservoir_lonlat: tuple[float, float] | None = None,
    *,
    dem_filename: str = "dem_srtm_gl1.tif",
    landcover_filename: str = "landcover_esa_worldcover.tif",
    floor_half_width_m: float = 0.0,
) -> ValleyGeometry:
    """Write a synthetic raw DEM + landcover raster (UTM, `RASTER_RES_M`) covering `bbox_deg`
    (EPSG:4326) into `raw_dir`. Returns the `ValleyGeometry` used, so tests can compute expected
    values at arbitrary points."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = bbox_deg
    corners = [(west, south), (west, north), (east, south), (east, north)]
    xs, ys = zip(*[lonlat_to_utm(lon, lat) for lon, lat in corners])
    pad = RASTER_RES_M * 3
    minx, maxx, miny, maxy = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    width = int(np.ceil((maxx - minx) / RASTER_RES_M))
    height = int(np.ceil((maxy - miny) / RASTER_RES_M))
    transform = Affine(RASTER_RES_M, 0.0, minx, 0.0, -RASTER_RES_M, maxy)

    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    x = minx + (cols + 0.5) * RASTER_RES_M
    y = maxy - (rows + 0.5) * RASTER_RES_M

    geom = valley_geometry(breach_lonlat, downstream_lonlat)
    lake_center = lonlat_to_utm(*lake_lonlat)
    reservoir_center = lonlat_to_utm(*reservoir_lonlat) if reservoir_lonlat else None

    dem = elevation(x, y, geom, lake_center=lake_center, reservoir_center=reservoir_center,
                    floor_half_width_m=floor_half_width_m).astype(np.float32)
    landcover = landcover_class(x, y, geom, lake_center=lake_center, reservoir_center=reservoir_center)

    r0, r1 = int(height * VOID_ROW_FRAC[0]), int(height * VOID_ROW_FRAC[1])
    c0, c1 = int(width * VOID_COL_FRAC[0]), int(width * VOID_COL_FRAC[1])
    dem[r0:r1, c0:c1] = -9999.0

    crs = CRS.from_epsg(UTM_EPSG)
    with rasterio.open(raw_dir / dem_filename, "w", driver="GTiff", width=width, height=height,
                        count=1, dtype=np.float32, crs=crs, transform=transform, nodata=-9999.0) as dst:
        dst.write(dem, 1)
    with rasterio.open(raw_dir / landcover_filename, "w", driver="GTiff", width=width, height=height,
                        count=1, dtype=np.uint8, crs=crs, transform=transform, nodata=255) as dst:
        dst.write(landcover, 1)

    return geom


def write_raw_provenance(raw_dir: Path, dem_product: str, dem_filename: str = "dem_srtm_gl1.tif",
                          landcover_filename: str = "landcover_esa_worldcover.tif") -> None:
    entry = {
        f"dem_{dem_product}": {
            "file": dem_filename, "status": "fetched", "dataset": dem_product, "source": "src_033",
            "native_crs": f"EPSG:{UTM_EPSG}", "native_resolution_m": RASTER_RES_M, "vertical_datum": "EGM96",
        },
        "landcover_esa_worldcover": {
            "file": landcover_filename, "status": "fetched", "dataset": "esa_worldcover_2021_v200",
            "source": "src_036", "native_resolution_m": 10,
        },
    }
    (Path(raw_dir) / "provenance.json").write_text(json.dumps(entry, indent=2), encoding="utf-8")

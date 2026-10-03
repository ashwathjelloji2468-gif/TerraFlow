"""M4: DualSPHysics near-field case generation (`docs/handoff_contract.md` §4.4).

`pilot_case_spec()` reproduces the pilot calibration run (`vram_estimator.py`, `docs/decisions.md`
2026-09-25 "M4 pilot case") of the stock DualSPHysics 5.4.3 example
`examples/main/01_DamBreak/CaseDambreakVal2D_Def.xml`) as a `case_xml.CaseSpec`, to check the
writer against a real, GenCase-validated file (`tests/m4_sph/test_pilot_regen.py`) before it is
used to build real near-field cases.
"""

from __future__ import annotations

import json
import math
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyproj
import rasterio

from backend.m2_breach.hydrograph import hydrograph as m2_hydrograph
from backend.m4_sph import vram_estimator
from backend.shared.grid import FLOAT_NODATA, CanonicalGrid, lonlat_to_rowcol
from backend.shared.probes import Probe, load_probes
from backend.shared.site_config import SiteConfig, default_data_dir, load_site_config

from .case_xml import CaseSpec, E, InOutZone, SwlGauge, TimeValue, VelocityGauge, write_case_xml
from .settings import SphSettings, load_sph_settings

CONTRACT_VERSION = "0.3.0"


class InflowUnavailable(Exception):
    """Raised when a scenario's near-field inflow can't be evaluated yet (e.g. `inflow.from:
    far_field` needs a far-field discharge series that only M3 can produce)."""


class OverVramBudget(Exception):
    """Raised when a case's predicted particle count would exceed the configured VRAM budget."""


def hydrograph_to_velocity(
    t_s: np.ndarray, q_m3s: np.ndarray, area_m2: float, t_start_s: float, t_end_s: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert a discharge hydrograph `Q(t)` (contract §4.2, seconds since t0) to a uniform inlet
    velocity `v(tau) = Q(t_start_s + tau) / area_m2` over a fixed inlet cross-section (m/s), for
    DualSPHysics `imposevelocity mode="1"` (`velocitytimes`, `time`-tagged in solver time `tau`,
    which starts at 0 at `t_start_s`).

    `t_s` must be sorted and span `[t_start_s, t_end_s]` (or the hydrograph's own end, if
    `t_end_s` is None); the window is clipped with linear interpolation at both ends.
    """
    if area_m2 <= 0:
        raise ValueError(f"area_m2 must be positive, got {area_m2}")
    t_s = np.asarray(t_s, dtype=float)
    q_m3s = np.asarray(q_m3s, dtype=float)
    if t_s.ndim != 1 or t_s.shape != q_m3s.shape or len(t_s) < 2:
        raise ValueError("t_s and q_m3s must be 1-D, equal-length, and have at least 2 samples")
    if np.any(np.diff(t_s) <= 0):
        raise ValueError("t_s must be strictly increasing")

    end = t_end_s if t_end_s is not None else float(t_s[-1])
    if end <= t_start_s:
        raise ValueError(f"t_end_s ({end}) must be greater than t_start_s ({t_start_s})")
    if t_start_s < t_s[0] or end > t_s[-1]:
        raise ValueError(
            f"window [{t_start_s}, {end}] s falls outside the hydrograph's range [{t_s[0]}, {t_s[-1]}] s"
        )

    mask = (t_s > t_start_s) & (t_s < end)
    window_t = np.concatenate(([t_start_s], t_s[mask], [end]))
    window_q = np.interp(window_t, t_s, q_m3s)

    tau_s = window_t - t_start_s
    v_ms = window_q / area_m2
    return tau_s, v_ms


@dataclass(frozen=True)
class InletGeometry:
    """A vertical rectangular inlet, `width_m` x `height_m`, centred on the inflow point (SPH
    frame, §1.3), bottom at bed elevation. `direction_xyz`/`rotate_deg` orient it perpendicular
    to the local channel tangent. The shipped DualSPHysics build's `rotateaxis` is clockwise;
    the sign is checked against a real solver-generated inlet-geometry error artifact."""

    x_utm_m: float
    y_utm_m: float
    point_xyz: tuple[float, float, float]
    size_xyz: tuple[float, float, float]
    direction_xyz: tuple[float, float, float]
    rotate_deg: float
    area_m2: float
    bed_z_m: float
    zsurf_m: float
    local_terrain_max_z_m: float | None = None
    clearance_m: float = 0.0
    effective_bottom_z_m: float | None = None
    effective_height_m: float | None = None


class InletClearanceError(ValueError):
    """Not enough vertical space between the terrain under the inlet footprint (+ clearance)
    and the target free surface for a valid inlet buffer."""


def _tangent_at_point(centreline, x_m: float, y_m: float, ds: float = 1.0) -> tuple[float, float]:
    """Unit downstream tangent `(dx, dy)` of a shapely `LineString` (site UTM) nearest `(x_m, y_m)`."""
    from shapely.geometry import Point

    s = centreline.project(Point(x_m, y_m))
    s0, s1 = max(0.0, s - ds), min(centreline.length, s + ds)
    if s1 <= s0:
        raise ValueError("centreline is too short to estimate a tangent near this point")
    p0, p1 = centreline.interpolate(s0), centreline.interpolate(s1)
    dx, dy = p1.x - p0.x, p1.y - p0.y
    norm = math.hypot(dx, dy)
    if norm == 0:
        raise ValueError("degenerate centreline tangent (coincident points)")
    return dx / norm, dy / norm


def inlet_rotation_for_tangent(tx: float, ty: float) -> float:
    """DualSPHysics clockwise rotation placing the inlet face normal to downstream flow."""
    # Its +x width axis rotates clockwise; this sign makes its width axis the channel normal
    # and its unrotated (0,-1) flow direction the downstream tangent.
    return -math.degrees(math.atan2(tx, -ty))


def inlet_geometry(
    inflow_lon_lat: tuple[float, float],
    grid_near: CanonicalGrid,
    dem_near: np.ndarray,
    frame: dict,
    centreline,
    width_m: float,
    height_m: float,
    *,
    dp_m: float | None = None,
    layers: int = 0,
    clearance_m: float = 0.0,
    min_height_m: float = 0.0,
) -> InletGeometry:
    """Place the inlet at `inflow_lon_lat` (site config `domains.near_field.inflow.location`).

    The target free surface is `zsurf = bed(centre) + height_m`. With `dp_m` given, the inlet
    buffer's bottom is raised to `max terrain under the rotated inlet footprint (plane + layers
    behind it) + clearance_m`, keeping `zsurf`; the effective height/area shrink accordingly and
    the inflow velocity must use the effective area. Raises `InletClearanceError` if the effective
    height is below `min_height_m`."""
    lon, lat = inflow_lon_lat
    x_utm, y_utm = pyproj.Transformer.from_crs(4326, grid_near.crs_epsg, always_xy=True).transform(lon, lat)

    row, col = lonlat_to_rowcol(grid_near, lon, lat)
    bed_z = float(dem_near[row, col])
    if bed_z == FLOAT_NODATA:
        raise ValueError(f"inflow location ({lon}, {lat}) is nodata in dem_nearfield.tif")

    tx, ty = _tangent_at_point(centreline, x_utm, y_utm)
    # Unrotated flow is (0,-1,0). DualSPHysics applies `rotateaxis` clockwise, so
    # `inlet_rotation_for_tangent` supplies the sign that maps that vector to (tx, ty).
    # The convention was confirmed from Teesta's solver-generated CfgInOut_ErrorParticles.vtk.
    rotate_deg = inlet_rotation_for_tangent(tx, ty)

    x_local = x_utm - frame["origin_x"]
    y_local = y_utm - frame["origin_y"]
    zsurf = bed_z + height_m
    bottom, local_max = bed_z, None
    if dp_m is not None:
        local_max = terrain_max_under_zone((x_local - width_m / 2, y_local, bed_z), (width_m, 0.0, height_m),
                                           rotate_deg, (x_local, y_local), layers, dp_m, grid_near, dem_near, frame)
        bottom = max(bed_z, local_max + clearance_m)
    eff_height = zsurf - bottom
    if eff_height < min_height_m or eff_height <= 0:
        raise InletClearanceError(
            f"inlet at ({lon}, {lat}) (UTM {x_utm:.2f}, {y_utm:.2f}): local terrain max {local_max} m + clearance "
            f"{clearance_m} m gives bottom {bottom:.3f} m; target zsurf {zsurf:.3f} m leaves effective height "
            f"{eff_height:.3f} m < minimum {min_height_m} m")
    return InletGeometry(
        x_utm_m=x_utm, y_utm_m=y_utm,
        point_xyz=(x_local - width_m / 2, y_local, bottom),
        size_xyz=(width_m, 0.0, eff_height),
        direction_xyz=(0.0, -1.0, 0.0),
        rotate_deg=rotate_deg,
        area_m2=width_m * eff_height,
        bed_z_m=bed_z,
        zsurf_m=zsurf,
        local_terrain_max_z_m=local_max,
        clearance_m=clearance_m if dp_m is not None else 0.0,
        effective_bottom_z_m=bottom,
        effective_height_m=eff_height,
    )


@dataclass(frozen=True)
class OutletGeometry:
    """A vertical rectangular outlet, downstream of the inlet along the centreline, `height_m`
    tall from its own local bed -- the same depth ceiling assumed for the inlet
    (`settings.inlet_height_m`), not the domain's full terrain relief, since the near-field crop
    can include valley walls far taller than any plausible flood surface; sizing to those would
    blow the particle/VRAM budget for no modelling benefit. `direction_xyz`/`rotate_deg`
    point *upstream* (into the domain from the outlet's own wall) -- the same "direction points
    into the fluid domain" convention as the inlet, whose direction points downstream (into the
    domain from the inlet's wall); see `outlet_geometry`'s docstring for why."""

    x_utm_m: float
    y_utm_m: float
    point_xyz: tuple[float, float, float]
    size_xyz: tuple[float, float, float]
    direction_xyz: tuple[float, float, float]
    rotate_deg: float
    bed_z_m: float
    zsurf0_m: float


def _box_corners_within_bounds(
    x_local: float, y_local: float, width_m: float, rotate_deg: float,
    domain_x_m: float, domain_y_m: float,
) -> bool:
    """True if both ends of a `width_m`-wide box centred at `(x_local, y_local)`, rotated
    `rotate_deg` about that centre (DualSPHysics's clockwise-for-positive-angle `rotateaxis`
    convention, confirmed against a real solver log: `x'=x*cos(a)+y*sin(a)`,
    `y'=-x*sin(a)+y*cos(a)`, relative to the centre), land inside `[0, domain_x_m] x
    [0, domain_y_m]` -- the same local-frame box GenCase's `JSphInOutPoints::CheckPoints`
    rejects a case over if an inlet/outlet point falls outside it."""
    angle = math.radians(rotate_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    half = width_m / 2
    for dx in (-half, half):
        cx = x_local + dx * cos_a
        cy = y_local - dx * sin_a
        if not (0.0 <= cx <= domain_x_m and 0.0 <= cy <= domain_y_m):
            return False
    return True


class InOutOutsideDomain(ValueError):
    """An inlet/outlet zone point (plane corner or particle-layer corner) lies outside the
    domain DualSPHysics will actually build -- it would abort at "Initialising InOut"."""


def generated_domain(grid_near: CanonicalGrid, dem_near: np.ndarray, dp_m: float, boundary_layers: int) -> dict:
    """The particle domain GenCase actually generates, in the SPH frame (metres).

    DualSPHysics's default simulation domain is the bounding box of the generated particles, not
    the `pointmin/pointmax` drawing box: `nearfield.stl` spans near-field *cell centres*
    (`m1_terrain.stl.build_triangles`), i.e. half a cell inside the grid edge, and boundary layers
    are drawn `boundary_layers * dp` below the terrain. Particles then snap to the dp lattice."""
    valid = dem_near[dem_near != FLOAT_NODATA]
    half = grid_near.cell_size_m / 2
    return {"x": (half, grid_near.width * grid_near.cell_size_m - half),
            "y": (half, grid_near.height * grid_near.cell_size_m - half),
            "z": (float(valid.min()) - (boundary_layers - 1) * dp_m if valid.size else 0.0,
                  float(valid.max()) if valid.size else 0.0)}


def zone_envelope(point_xyz, size_xyz, rotate_deg: float, rotate_center_xy, layers: int, dp_m: float) -> np.ndarray:
    """`(8, 3)` corners of an inout zone *including* its particle layers, which DualSPHysics
    builds `layers * dp` behind the zone plane (opposite `direction`, i.e. +y before rotation for
    this module's `(0, -1, 0)` zones), rotated with the same clockwise `rotateaxis` convention
    as `_box_corners_within_bounds`."""
    px, py, pz = point_xyz
    sx, _, sz = size_xyz
    cx, cy = rotate_center_xy if rotate_center_xy is not None else (px + sx / 2, py)
    a = math.radians(rotate_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    corners = []
    for x in (px, px + sx):
        for y in (py, py + layers * dp_m):
            dx, dy = x - cx, y - cy
            for z in (pz, pz + sz):
                corners.append((cx + dx * cos_a + dy * sin_a, cy - dx * sin_a + dy * cos_a, z))
    return np.asarray(corners)


def envelope_violations(corners: np.ndarray, domain: dict, margin_m: float) -> list[str]:
    """Human-readable violations: which side, which coordinate, by how much."""
    out = []
    for axis, i in (("x", 0), ("y", 1), ("z", 2)):
        lo, hi = domain[axis][0] + margin_m, domain[axis][1] - margin_m
        if corners[:, i].min() < lo:
            out.append(f"{axis}-min side: {corners[:, i].min():.3f} < {lo:.3f}")
        if corners[:, i].max() > hi:
            out.append(f"{axis}-max side: {corners[:, i].max():.3f} > {hi:.3f}")
    return out


def validate_inout_within_domain(zones: list[tuple[str, InOutZone]], dp_m: float, domain: dict,
                                 margin_m: float) -> None:
    """Raise `InOutOutsideDomain` naming the zone, side and coordinate if any point of any zone
    (plane or layers) is outside `domain` shrunk by `margin_m`. Never moves a zone."""
    problems = []
    for name, z in zones:
        corners = zone_envelope(z.point_xyz, z.size_xyz, z.rotate_deg, z.rotate_center_xy, z.layers, dp_m)
        problems += [f"{name} zone {v}" for v in envelope_violations(corners, domain, margin_m)]
    if problems:
        raise InOutOutsideDomain(
            "inout zone outside the generated particle domain "
            f"(x {domain['x'][0]:.3f}..{domain['x'][1]:.3f}, y {domain['y'][0]:.3f}..{domain['y'][1]:.3f}, "
            f"z {domain['z'][0]:.3f}..{domain['z'][1]:.3f}, margin {margin_m:.3f} m): " + "; ".join(problems))


def terrain_max_under_zone(point_xyz, size_xyz, rotate_deg: float, rotate_center_xy, layers: int, dp_m: float,
                           grid_near: CanonicalGrid, dem_near: np.ndarray, frame: dict) -> float:
    """Highest terrain under a zone's rotated footprint (plane + `layers * dp` behind it).

    `nearfield.stl` is a triangulated surface through near-field *cell centres*, so between
    centres the solid surface is a linear interpolation of neighbouring cell values. Every cell
    centre within one cell size of the footprint is taken (all vertices of every STL triangle
    the footprint can touch), which bounds the surface from above -- a conservative maximum."""
    from shapely.geometry import MultiPoint, Point

    corners = zone_envelope(point_xyz, size_xyz, rotate_deg, rotate_center_xy, layers, dp_m)
    footprint = MultiPoint([(float(x), float(y)) for x, y in corners[:, :2]]).convex_hull.buffer(grid_near.cell_size_m)
    cs = grid_near.cell_size_m
    minx, miny, maxx, maxy = footprint.bounds
    # local x = origin_x + (col + 0.5) * cs - frame_x ; local y = origin_y - (row + 0.5) * cs - frame_y
    ox, oy = grid_near.origin_x - frame["origin_x"], grid_near.origin_y - frame["origin_y"]
    c0, c1 = max(0, int(math.floor((minx - ox) / cs))), min(grid_near.width - 1, int(math.ceil((maxx - ox) / cs)))
    r0, r1 = max(0, int(math.floor((oy - maxy) / cs))), min(grid_near.height - 1, int(math.ceil((oy - miny) / cs)))
    values = [float(dem_near[r, c]) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
              if dem_near[r, c] != FLOAT_NODATA and footprint.covers(Point(ox + (c + 0.5) * cs, oy - (r + 0.5) * cs))]
    if not values:
        raise InletClearanceError("no valid terrain under the inlet footprint")
    return max(values)


def outlet_geometry(
    inflow_lon_lat: tuple[float, float],
    grid_near: CanonicalGrid,
    dem_near: np.ndarray,
    frame: dict,
    centreline,
    width_m: float,
    height_m: float,
    margin_m: float,
    *,
    dp_m: float | None = None,
    layers: int = 0,
    domain: dict | None = None,
) -> OutletGeometry:
    """Place a downstream outlet cross-section `margin_m` inside the near-field grid's downstream
    edge from the inflow point, walking the centreline in its increasing-arc-length ("downstream")
    direction -- the same convention `inlet_geometry`/`_tangent_at_point` already assume.

    Without an outlet, injected fluid has nowhere to exit and ponds at the inlet, where the
    inlet's own `inputtreatment=2` ("Remove fluid" -- DualSPHysics's own recommended inlet
    setting, `examples/inletoutlet/02_OpenChannel`) deletes any fluid particle later found back
    inside the inlet's footprint. That example always pairs its inlet with a downstream outlet
    (`inputtreatment=1`, "Convert fluid"); this function builds that missing second zone
    (`docs/decisions.md` "M4: SPH outlet zone" -- diagnosed from the excluded-particle position
    dump for `teesta_2023_mvp__dualsphysics` attempt a02, which placed 100% of the run's `NpOutPos`
    exclusions at the inlet corner, not the domain edge).

    The outlet's `direction` points *upstream* (into the domain from its own wall), the mirror of
    the inlet's downstream-pointing direction, matching the stock example's inlet/outlet direction
    pair (`"right"` in vs `"left"` out in `02_OpenChannel`'s 2-D case): a box zone's `direction`
    always points from the zone into the fluid domain, and the domain lies upstream of the outlet.
    """
    from shapely.geometry import Point

    lon, lat = inflow_lon_lat
    x_utm, y_utm = pyproj.Transformer.from_crs(4326, grid_near.crs_epsg, always_xy=True).transform(lon, lat)
    s0 = centreline.project(Point(x_utm, y_utm))
    left, bottom, right, top = grid_near.bounds
    domain_x_m = grid_near.width * grid_near.cell_size_m
    domain_y_m = grid_near.height * grid_near.cell_size_m

    step = max(grid_near.cell_size_m, 1.0)
    last_inside_s = None
    s = s0
    while s <= centreline.length:
        p = centreline.interpolate(s)
        if left <= p.x <= right and bottom <= p.y <= top:
            last_inside_s = s
            s += step
        else:
            break
    if last_inside_s is None or last_inside_s <= s0:
        raise ValueError(
            "centreline does not extend downstream of the inflow point within the near-field grid"
        )

    # Back off from the grid's downstream edge, then keep backing off (bounded by s0) until the
    # box's *rotated* footprint -- not just its centreline point -- fits inside the local domain:
    # a box this wide (`width_m`) can swing a corner outside the grid even when its own centre,
    # margin_m inside the edge, does not (diagnosed by GenCase's `JSphInOutPoints::CheckPoints`
    # exception on the first outlet placement tried here, which put a rotated corner at local
    # x=632 against a 600 m-wide domain -- docs/decisions.md "M4: SPH outlet zone").
    s_outlet = max(s0, last_inside_s - margin_m)
    bed_z = FLOAT_NODATA
    x_local = y_local = rotate_deg = None
    while s_outlet > s0:
        outlet_pt = centreline.interpolate(s_outlet)
        col_i = int((outlet_pt.x - grid_near.origin_x) / grid_near.cell_size_m)
        row_i = int((grid_near.origin_y - outlet_pt.y) / grid_near.cell_size_m)
        row_i = min(max(row_i, 0), grid_near.height - 1)
        col_i = min(max(col_i, 0), grid_near.width - 1)
        candidate_bed_z = float(dem_near[row_i, col_i])
        if candidate_bed_z != FLOAT_NODATA:
            tx, ty = _tangent_at_point(centreline, outlet_pt.x, outlet_pt.y)
            candidate_rotate_deg = inlet_rotation_for_tangent(-tx, -ty)  # points upstream
            candidate_x_local = outlet_pt.x - frame["origin_x"]
            candidate_y_local = outlet_pt.y - frame["origin_y"]
            if domain is not None and dp_m is not None:
                # The whole zone -- plane *and* its `layers * dp` particle layers -- must fit the
                # particle domain GenCase actually generates (`generated_domain`), with one dp of
                # margin; checking only the plane ends against the grid edge let a layer corner
                # land outside it (DualSPHysics 5.4.355: "Point for inlet conditions ... is
                # outside the domain" on the synthetic case's outlet).
                envelope = zone_envelope((candidate_x_local - width_m / 2, candidate_y_local, candidate_bed_z),
                                         (width_m, 0.0, height_m), candidate_rotate_deg,
                                         (candidate_x_local, candidate_y_local), layers, dp_m)
                fits = not envelope_violations(envelope, domain, dp_m)
            else:
                fits = _box_corners_within_bounds(candidate_x_local, candidate_y_local, width_m,
                                                  candidate_rotate_deg, domain_x_m, domain_y_m)
            if fits:
                bed_z, x_local, y_local, rotate_deg = (
                    candidate_bed_z, candidate_x_local, candidate_y_local, candidate_rotate_deg,
                )
                break
        s_outlet -= step
    if bed_z == FLOAT_NODATA or x_local is None:
        raise ValueError(
            "no outlet placement along the centreline (valid terrain, rotated box within the "
            "near-field domain) found downstream of the inflow point"
        )

    return OutletGeometry(
        x_utm_m=x_local + frame["origin_x"], y_utm_m=y_local + frame["origin_y"],
        point_xyz=(x_local - width_m / 2, y_local, bed_z),
        size_xyz=(width_m, 0.0, height_m),
        direction_xyz=(0.0, -1.0, 0.0),
        rotate_deg=rotate_deg,
        bed_z_m=bed_z,
        zsurf0_m=bed_z + height_m / 2,
    )


@dataclass(frozen=True)
class NearfieldProbe:
    probe: Probe
    x_m: float  # SPH frame
    y_m: float
    z_bed_m: float


def probes_in_nearfield(
    probes: list[Probe], frame: dict, grid_near: CanonicalGrid, dem_near: np.ndarray,
) -> tuple[list[NearfieldProbe], list[str]]:
    """Split `probes` (site-wide, from `pois.gpkg`) into the ones that fall inside the near-field
    grid (kept, with SPH-frame coordinates and bed elevation) and the `poi_id`s of the rest."""
    kept: list[NearfieldProbe] = []
    skipped: list[str] = []
    left, bottom, right, top = grid_near.bounds
    for p in probes:
        if not (left <= p.x_m <= right and bottom <= p.y_m <= top):
            skipped.append(p.poi_id)
            continue
        # p.x_m/p.y_m are already UTM; convert to near-field row/col directly (`lonlat_to_rowcol`
        # takes lon/lat, not UTM metres).
        col_i = int((p.x_m - grid_near.origin_x) / grid_near.cell_size_m)
        row_i = int((grid_near.origin_y - p.y_m) / grid_near.cell_size_m)
        row_i = min(max(row_i, 0), grid_near.height - 1)
        col_i = min(max(col_i, 0), grid_near.width - 1)
        z_bed = float(dem_near[row_i, col_i])
        if z_bed == FLOAT_NODATA:
            skipped.append(p.poi_id)
            continue
        kept.append(NearfieldProbe(
            probe=p, x_m=p.x_m - frame["origin_x"], y_m=p.y_m - frame["origin_y"], z_bed_m=z_bed,
        ))
    return kept, skipped


def check_vram(
    dp_m: float, domain_x_m: float, domain_y_m: float, fluid_depth_m: float,
    settings: SphSettings, *, allow_over_budget: bool = False,
) -> dict:
    """Estimate the particle count/VRAM for a case at `dp_m` (`backend/m4_sph/vram_estimator.py`,
    calibrated from the pilot run) and raise `OverVramBudget` unless it fits `settings`'s budget."""
    cal = vram_estimator.calibrate()
    counts = vram_estimator.estimate_particle_count(
        domain_x_m, domain_y_m, fluid_depth_m, dp_m, boundary_layers=settings.boundary_layers,
    )
    vram_mib = vram_estimator.estimate_vram_mib(counts["total_particles"], cal)
    usable_mib = settings.vram_budget_mib * (1 - settings.vram_margin)
    result = {**counts, "vram_mib": vram_mib, "budget_mib": usable_mib, "dp_m": dp_m}
    if vram_mib > usable_mib and not allow_over_budget:
        raise OverVramBudget(
            f"dp={dp_m} m -> {counts['total_particles']:,.0f} particles, {vram_mib:,.0f} MiB "
            f"predicted, exceeds the {usable_mib:,.0f} MiB budget "
            f"({settings.vram_budget_mib} MiB * (1 - {settings.vram_margin}))"
        )
    return result


def build_nearfield_case(
    site_id: str, scenario_id: str, params: dict, settings: SphSettings | None = None,
    data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
    routed_discharge_path: str | Path | None = None,
    terrain_dir: str | Path | None = None,
    production: bool = False,
) -> tuple[CaseSpec, dict]:
    """Build the near-field GenCase spec for `scenario_id` on `site_id`, from M1 terrain
    (`data/<site_id>/terrain/`) and an M2 hydrograph for `params`. Returns `(spec, case_meta)`.

    `params` is a scenario's breach parameters (contract §4.3): `water_volume_m3`,
    `breach_width_m`, `failure_time_s`, plus whatever `hydrograph()` needs for the triggering dam.
    `sites_dir` overrides where `<site_id>.yaml` is loaded from (default `sites/`), for tests.

    Feature 17: a `routed_discharge_path` is fully validated before use
    (`routed_discharge.validate_routed_discharge`: schema, usable status, sha256, series, section CRS =
    the site's UTM zone, coverage of the configured `[t_start_s, t_end_s]` window, and the source M3
    run's existence and class -- ACCEPTED/PILOT, or ACCEPTED + `artifact_validated` when
    `production=True`). Any failure raises `RoutedDischargeInvalid`; there is no fallback to a typed
    or M2 discharge in the routed path. `case_meta.provenance.routed_discharge_artifact` records which
    artifact (path + manifest/timeseries sha256) fed the case.
    """
    settings = settings or load_sph_settings()
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()  # $SIH26_DATA_DIR, else <repo>/data
    cfg = load_site_config(site_id, sites_dir=sites_dir)
    terrain_dir = Path(terrain_dir) if terrain_dir is not None else data_dir / site_id / "terrain"

    grid_near = CanonicalGrid.from_json(terrain_dir / "grid_nearfield.json")
    frame = json.loads((terrain_dir / "nearfield_frame.json").read_text(encoding="utf-8"))
    with rasterio.open(terrain_dir / "dem_nearfield.tif") as ds:
        dem_near = ds.read(1)
    centreline = gpd.read_file(terrain_dir / "centreline.gpkg").geometry.iloc[0]

    nf = cfg.domains.near_field
    dam_id = nf.inflow.from_
    routed_record = None
    routed_report = None
    if routed_discharge_path is not None:
        from backend.m3_dflowfm import routed_discharge as rd
        rd.check_scenario_id(scenario_id)
        hydro_t_s, hydro_q_m3s, routed_record, routed_report = rd.validate_routed_discharge(
            routed_discharge_path, site_id=site_id, scenario_id=scenario_id,
            expected_epsg=int(cfg.crs.utm_epsg.value),
            allowed_statuses=rd.PRODUCTION_STATUSES if production else rd.USABLE_STATUSES,
            window_s=(settings.t_start_s, settings.t_end_s),
            runs_dir=data_dir / site_id / "runs",
            source_run_statuses=("ACCEPTED",) if production else ("ACCEPTED", "PILOT"),
        )
        hydro_method = "m3_routed_discharge"
    elif dam_id == "far_field":
        raise InflowUnavailable(
            f"{site_id}: near_field.inflow.from is 'far_field' -- the SPH inlet needs a routed "
            f"far-field discharge series, which only M3 (Delft3D) can produce; not available yet."
        )

    else:
        hydro = m2_hydrograph(site_id, dam_id, params, sites_dir=sites_dir)
        hydro_t_s, hydro_q_m3s = hydro.t_s, hydro.q_m3s
        hydro_method = hydro.method
    t_start_s = settings.t_start_s
    t_end_s = settings.t_end_s if settings.t_end_s is not None else float(hydro_t_s[-1])

    inflow_location = nf.inflow.location.value
    dp_setting = settings.dp_m
    left, bottom, right, top = grid_near.bounds
    domain_x_m, domain_y_m = right - left, top - bottom
    fluid_depth_m = settings.inlet_height_m
    if dp_setting == "auto":
        auto = vram_estimator.smallest_dp_within_budget(
            domain_x_m, domain_y_m, fluid_depth_m,
            vram_estimator.calibrate(), vram_budget_mib=settings.vram_budget_mib,
            margin=settings.vram_margin, boundary_layers=settings.boundary_layers,
        )
        if not auto["feasible"]:
            raise OverVramBudget(f"{site_id}/{scenario_id}: {auto.get('reason', 'no feasible dp found')}")
        dp_m = auto["dp_m"]
        vram_info = {"dp_m": dp_m, "total_particles": auto["total_particles"], "vram_mib": auto["predicted_vram_mib"], "budget_mib": auto["budget_mib"]}
    else:
        dp_m = float(dp_setting)
        vram_info = check_vram(dp_m, domain_x_m, domain_y_m, fluid_depth_m, settings)

    try:
        inlet = inlet_geometry(
            tuple(inflow_location), grid_near, dem_near, frame, centreline,
            settings.inlet_width_m, settings.inlet_height_m,
            dp_m=dp_m, layers=settings.inlet_layers,
            clearance_m=settings.inlet_terrain_clearance_dp * dp_m,
            min_height_m=settings.min_inlet_height_dp * dp_m,
        )
    except InletClearanceError as e:
        raise InletClearanceError(f"{site_id}/{scenario_id}: {e}") from None

    tau_s, v_ms = hydrograph_to_velocity(hydro_t_s, hydro_q_m3s, inlet.area_m2, t_start_s, t_end_s)
    velocity_times = [TimeValue(float(t), float(v)) for t, v in zip(tau_s, v_ms)]
    inout_zone = InOutZone(
        point_xyz=inlet.point_xyz, size_xyz=inlet.size_xyz, direction_xyz=inlet.direction_xyz,
        velocity_times=velocity_times, zsurf_m=inlet.zsurf_m, layers=settings.inlet_layers,
        rotate_deg=inlet.rotate_deg,
        rotate_center_xy=(inlet.point_xyz[0] + inlet.size_xyz[0] / 2, inlet.point_xyz[1]),
    )

    dem_valid = dem_near[dem_near != FLOAT_NODATA]
    z_min = float(dem_valid.min()) if dem_valid.size else 0.0
    z_max = max(float(dem_valid.max()) if dem_valid.size else 0.0, inlet.zsurf_m)


    # The domain DualSPHysics will actually build; every inout point must lie inside it.
    domain = generated_domain(grid_near, dem_near, dp_m, settings.boundary_layers)
    outlet = outlet_geometry(
        tuple(inflow_location), grid_near, dem_near, frame, centreline,
        settings.inlet_width_m, settings.inlet_height_m, settings.outlet_margin_m,
        dp_m=dp_m, layers=settings.inlet_layers, domain=domain,
    )
    outlet_zone = InOutZone(
        point_xyz=outlet.point_xyz, size_xyz=outlet.size_xyz, direction_xyz=outlet.direction_xyz,
        layers=settings.inlet_layers, refilling=2, inputtreatment=1,
        rotate_deg=outlet.rotate_deg,
        rotate_center_xy=(outlet.point_xyz[0] + outlet.size_xyz[0] / 2, outlet.point_xyz[1]),
        velocity_mode=2, imposerhop_mode=1, zsurf_mode=2, zsurf_m=outlet.zsurf0_m,
    )

    probes = load_probes(terrain_dir)
    kept_probes, skipped_probes = probes_in_nearfield(probes, frame, grid_near, dem_near)


    draw_commands = [
        E("setmkfluid", {"mk": 0}),
        E("setmkbound", {"mk": 0}),  # mkconfig boundcount=1 below -> only mk 0 is valid
    ]
    for layer in range(settings.boundary_layers):
        draw_commands.append(E("drawfilestl", {"file": "nearfield.stl"}, children=[
            E("drawmove", {"x": 0, "y": 0, "z": -layer * dp_m}),
        ]))

    vel_gauges = [
        VelocityGauge(f"vel_{p.probe.poi_id}", (p.x_m, p.y_m, p.z_bed_m + settings.probe_velocity_height_m))
        for p in kept_probes
    ]
    swl_gauges = [
        SwlGauge(
            f"swl_{p.probe.poi_id}",
            point0_xyz=(p.x_m, p.y_m, p.z_bed_m - dp_m),
            point2_xyz=(p.x_m, p.y_m, p.z_bed_m + settings.inlet_height_m),
        )
        for p in kept_probes
    ]

    spec = CaseSpec(
        dp_m=dp_m,
        pointref_xyz=(0.0, 0.0, 0.0),
        pointmin_xyz=(0.0, 0.0, z_min - settings.inlet_height_m),
        pointmax_xyz=(grid_near.width * grid_near.cell_size_m, grid_near.height * grid_near.cell_size_m, z_max),
        constants=[
            ("gravity", {"x": 0, "y": 0, "z": -9.81, "comment": "Gravitational acceleration", "units_comment": "m/s^2"}),
            ("rhop0", {"value": 1000, "comment": "Reference density of the fluid (clear water -- caveat clear_water)", "units_comment": "kg/m^3"}),
            ("rhopgradient", {"value": 2, "comment": "Initial density gradient 1:Rhop0, 2:Water column, 3:Max. water height (default=2)"}),
            ("hswl", {"value": settings.inlet_height_m, "auto": False,
                      "comment": "Still water level for speedofsound -- fixed, not auto: the case starts with "
                                 "no fluid (all inflow comes from the inlet zone at runtime), so GenCase's "
                                 "auto-detected still water level would be 0", "units_comment": "metres (m)"}),
            ("gamma", {"value": 7, "comment": "Polytropic constant for water used in the state equation"}),
            ("speedsystem", {"value": 0, "auto": True, "comment": "Maximum system speed"}),
            ("coefsound", {"value": 20, "comment": "Coefficient to multiply speedsystem"}),
            ("speedsound", {"value": 0, "auto": True, "comment": "Speed of sound to use in the simulation"}),
            ("coefh", {"value": 1.0, "comment": "Coefficient to calculate the smoothing length"}),
            ("cflnumber", {"value": 0.2, "comment": "Coefficient to multiply dt"}),
        ],
        mk_boundcount=1,
        mk_fluidcount=1,
        draw_commands=draw_commands,
        parameters=[
            ("StepAlgorithm", 1, "Step Algorithm 1:Verlet, 2:Symplectic (default=1) -- DualSPHysics 01_DamBreak default"),
            ("VerletSteps", 40, "Verlet only: Number of steps to apply Euler timestepping -- DualSPHysics 01_DamBreak default"),
            ("Kernel", 2, "Interaction Kernel 1:Cubic Spline, 2:Wendland -- DualSPHysics 01_DamBreak default"),
            ("ViscoTreatment", 1, "Viscosity formulation 1:Artificial, 2:Laminar+SPS, 3:Laminar -- DualSPHysics 01_DamBreak default"),
            ("Visco", 0.02, "Viscosity value -- DualSPHysics 01_DamBreak default"),
            ("DensityDT", 2, "Density Diffusion Term 0:None, 1:Molteni, 2:Fourtakas, 3:Fourtakas(full) -- DualSPHysics 01_DamBreak default"),
            ("DensityDTvalue", 0.1, "DDT value -- DualSPHysics 01_DamBreak default"),
            ("RigidAlgorithm", 1, "Rigid Algorithm 0:collision-free, 1:SPH, 2:DEM, 3:Chrono -- DualSPHysics 01_DamBreak default"),
            ("TimeMax", tau_s[-1], "Time of simulation"),
            ("TimeOut", settings.time_out_s, "Time out data"),
            ("RhopOutMin", 700, "Minimum rhop valid -- DualSPHysics 01_DamBreak default"),
            ("RhopOutMax", 1300, "Maximum rhop valid -- DualSPHysics 01_DamBreak default"),
        ],
        inout_zones=[inout_zone, outlet_zone],
        swl_gauges=swl_gauges,
        vel_gauges=vel_gauges,
    )

    # Fail before GenCase, naming the zone/side/coordinate, rather than inside the solver.
    # One dp of margin covers the particles' snap to the dp lattice; no zone is moved here.
    validate_inout_within_domain([("inlet", inout_zone), ("outlet", outlet_zone)], dp_m, domain, margin_m=dp_m)

    caveats = ["clear_water", "fixed_area_inlet"]
    case_meta = {
        "contract_version": CONTRACT_VERSION,
        "site_id": site_id, "scenario_id": scenario_id, "model": "sph",
        "t_start_s": t_start_s, "t_end_s": t_end_s,
        "dp_m": dp_m, **{f"vram_{k}": v for k, v in vram_info.items() if k != "dp_m"},
        "inlet": {
            "x_utm_m": inlet.x_utm_m, "y_utm_m": inlet.y_utm_m,
            "area_m2": inlet.area_m2, "bed_z_m": inlet.bed_z_m, "zsurf_m": inlet.zsurf_m,
            "rotate_deg": inlet.rotate_deg, "width_m": settings.inlet_width_m,
            # Source/physical inlet vs the solver buffer actually generated (Feature 6):
            "source_bed_z_m": inlet.bed_z_m, "target_zsurf_m": inlet.zsurf_m,
            "local_terrain_max_z_m": inlet.local_terrain_max_z_m, "terrain_clearance_m": inlet.clearance_m,
            "effective_bottom_z_m": inlet.effective_bottom_z_m, "effective_height_m": inlet.effective_height_m,
            "effective_area_m2": inlet.area_m2,
            "velocity_area_basis": "effective_area_m2",
        },
        "outlet": {
            "x_utm_m": outlet.x_utm_m, "y_utm_m": outlet.y_utm_m,
            "bed_z_m": outlet.bed_z_m, "zsurf0_m": outlet.zsurf0_m,
            "rotate_deg": outlet.rotate_deg,
        },
        "generated_domain_m": {k: list(v) for k, v in domain.items()},
        "inout_domain_margin_m": dp_m,
        "probes_used": [p.probe.poi_id for p in kept_probes],
        "probes_skipped": skipped_probes,
        "has_placeholders": cfg.has_placeholders,
        "placeholder_fields": cfg.placeholder_fields,
        "caveats": caveats,
        "provenance": {"method": "m4_sph.generator.build_nearfield_case", "hydrograph_method": hydro_method,
                       **({"routed_discharge": routed_record,
                           "routed_discharge_artifact": {**routed_report, "consumed_window_s": [t_start_s, t_end_s]}}
                          if routed_record is not None else {})},
        "production": bool(production),
    }
    return spec, case_meta


def write_case(spec: CaseSpec, case_meta: dict, run_dir: str | Path, terrain_dir: str | Path) -> Path:
    """Write `runs/<scenario_id>__sph/case/` (contract §4.4): the GenCase `_Def.xml`, a copy of
    `nearfield.stl`, `probes.csv` and `case_meta.json`. Never launches GenCase or the solver."""
    case_dir = Path(run_dir) / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    name = f"{case_meta['scenario_id']}__sph"
    write_case_xml(spec, case_dir / f"{name}_Def.xml")
    shutil.copyfile(Path(terrain_dir) / "nearfield.stl", case_dir / "nearfield.stl")
    (case_dir / "case_meta.json").write_text(json.dumps(case_meta, indent=2) + "\n", encoding="utf-8")
    return case_dir


def pilot_case_spec() -> CaseSpec:
    """`CaseDambreakVal2D` (Koshizuka & Oka 1996 dam-break validation), as shipped with
    DualSPHysics 5.4.3. 2D (`y` fixed at 0), no boundary STL, no inlet — used only to validate
    `case_xml`'s writer against a real GenCase file."""
    constants = [
        ("gravity", {"x": 0, "y": 0, "z": -9.81, "comment": "Gravitational acceleration", "units_comment": "m/s^2"}),
        ("rhop0", {"value": 1000, "comment": "Reference density of the fluid", "units_comment": "kg/m^3"}),
        ("rhopgradient", {"value": 2, "comment": "Initial density gradient 1:Rhop0, 2:Water column, 3:Max. water height (default=2)"}),
        ("hswl", {"value": 0, "auto": True, "comment": "Maximum still water level to calculate speedofsound using coefsound", "units_comment": "metres (m)"}),
        ("gamma", {"value": 7, "comment": "Polytropic constant for water used in the state equation"}),
        ("speedsystem", {"value": 0, "auto": True, "comment": "Maximum system speed (by default the dam-break propagation is used)"}),
        ("coefsound", {"value": 20, "comment": "Coefficient to multiply speedsystem"}),
        ("speedsound", {"value": 0, "auto": True, "comment": "Speed of sound to use in the simulation (by default speedofsound=coefsound*speedsystem)"}),
        ("coefh", {"value": 1.0, "comment": "Coefficient to calculate the smoothing length (h=coefh*sqrt(3*dp^2) in 3D)"}),
        ("_hdp", {"value": 2, "comment": "Alternative option to calculate the smoothing length (h=hdp*dp)"}),
        ("cflnumber", {"value": 0.2, "comment": "Coefficient to multiply dt"}),
    ]

    draw_commands = [
        E("setdrawmode", {"mode": "full"}),
        E("setmkfluid", {"mk": 0}),
        E("drawbox", children=[
            _text_el("boxfill", "solid"),
            E("point", {"x": 0, "y": -1, "z": 0}),
            E("size", {"x": 1, "y": 2, "z": 2}),
        ]),
        E("setmkbound", {"mk": 0}),
        E("drawbox", children=[
            _text_el("boxfill", "bottom | left | right | front | back"),
            E("point", {"x": 0, "y": -1, "z": 0}),
            E("size", {"x": 4, "y": 2, "z": 3}),
        ]),
    ]

    gauges_default = E("default", children=[
        E("savevtkpart", {"value": True, "comment": "Creates VTK files for each PART (default=false)"}),
        E("_computedt", {"value": 0.001, "comment": "Time between measurements. 0:all steps (default=TimeOut)", "units_comment": "s"}),
        E("_computetime", {"start": 0.1, "end": 0.2, "comment": "Start and end of measures. (default=simulation time)", "units_comment": "s"}),
        E("output", {"value": True, "comment": "Creates CSV files of measurements (default=false)"}),
        E("_outputdt", {"value": 0, "comment": "Time between output measurements. 0:all steps (default=TimeOut)", "units_comment": "s"}),
        E("_outputtime", {"start": 0, "end": 10, "comment": "Start and end of output measures. (default=simulation time)", "units_comment": "s"}),
    ])

    swl_gauge_1 = SwlGauge("Swl_x02", point0_xyz=(0.2, 0, -0.05), point2_xyz=(0.2, 0, 2.1))
    swl_gauge_2 = SwlGauge(
        "Swl_z003", point0_xyz=(-0.05, 0, 0.03), point2_xyz=(4.05, 0, 0.03),
        compute_dt_s=0.005, output_dt_s=0.005,
    )

    parameters = [
        ("SavePosDouble", 0, "Saves particle position using double precision (default=0)"),
        ("StepAlgorithm", 1, "Step Algorithm 1:Verlet, 2:Symplectic (default=1)"),
        ("VerletSteps", 40, "Verlet only: Number of steps to apply Euler timestepping (default=40)"),
        ("Kernel", 2, "Interaction Kernel 1:Cubic Spline, 2:Wendland (default=2)"),
        ("ViscoTreatment", 1, "Viscosity formulation 1:Artificial, 2:Laminar+SPS, 3:Laminar (default=1)"),
        ("Visco", 0.02, "Viscosity value. Typically 0.01 for Artificial and 1e-6 m^2/s (water kinematic viscosity) for Laminar"),
        ("ViscoBoundFactor", 1, "Multiply viscosity value with boundary (default=1)"),
        ("DensityDT", 2, "Density Diffusion Term 0:None, 1:Molteni, 2:Fourtakas, 3:Fourtakas(full) (default=0)"),
        ("DensityDTvalue", 0.1, "DDT value (default=0.1)"),
        ("Shifting", 0, "Shifting mode 0:None, 1:Ignore bound, 2:Ignore fixed, 3:Full (default=0)"),
        ("ShiftCoef", -2, "Coefficient for shifting computation (default=-2)"),
        ("ShiftTFS", 0, "Threshold to detect free surface. Typically 1.5 for 2D and 2.75 for 3D (default=0)"),
        ("RigidAlgorithm", 1, "Rigid Algorithm 0:collision-free, 1:SPH, 2:DEM, 3:Chrono (default=1)"),
        ("FtPause", 0.0, "Time to freeze the floatings at simulation start (warmup) (default=0)"),
        ("CoefDtMin", 0.05, "Coefficient to calculate minimum time step dtmin=coefdtmin*h/speedsound (default=0.05)"),
        ("DtIni", 0, "Initial time step. Use 0 to default use (default=h/speedsound)"),
        ("DtMin", 0, "Minimum time step. Use 0 to default use (default=coefdtmin*h/speedsound)"),
        ("DtFixed", 0, "Fixed Dt value. Use 0 to disable (default=disabled)"),
        ("DtFixedFile", "NONE", "Dt values are loaded from file. Use NONE to disable (default=disabled)"),
        ("DtAllParticles", 0, "Velocity of particles used to calculate DT. 1:All, 0:Only fluid/floating (default=0)"),
        ("TimeMax", 2.0, "Time of simulation"),
        ("TimeOut", 0.01, "Time out data"),
        ("PartsOutMax", 1, "%/100 of fluid particles allowed to be excluded from domain (default=1)"),
        ("RhopOutMin", 700, "Minimum rhop valid (default=700)"),
        ("RhopOutMax", 1300, "Maximum rhop valid (default=1300)"),
    ]

    return CaseSpec(
        dp_m=0.01,
        pointref_xyz=(0, 0, 0),
        pointmin_xyz=(-1, 0, -1),
        pointmax_xyz=(4.5, 0, 3.5),
        constants=constants,
        mk_boundcount=240,
        mk_fluidcount=9,
        draw_commands=draw_commands,
        parameters=parameters,
        gauges_default=gauges_default,
        swl_gauges=[swl_gauge_1, swl_gauge_2],
        simdomain_posmax=("default", "default", "default + 50%"),
    )


def _text_el(tag: str, text: str) -> ET.Element:
    el = ET.Element(tag)
    el.text = text
    return el


def main(argv: list[str] | None = None) -> None:
    """Write a near-field case's GenCase input files. Never launches GenCase or the solver
    (docs/decisions.md rule 14 -- long solver runs are the job system's job, not this CLI's)."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--params-json", required=True, help="JSON object: breach_width_m, failure_time_s, water_volume_m3, ...")
    parser.add_argument("--dp-m", type=float, default=None, help="override settings.dp_m (skips the VRAM search)")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--run-dir", default=None, help="default: <data-dir>/<site>/runs/<scenario-id>__sph")
    args = parser.parse_args(argv)

    params = json.loads(args.params_json)
    overrides = {"dp_m": args.dp_m} if args.dp_m is not None else {}
    settings = load_sph_settings(**overrides)
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()

    spec, case_meta = build_nearfield_case(args.site, args.scenario_id, params, settings, data_dir)
    run_dir = Path(args.run_dir) if args.run_dir else data_dir / args.site / "runs" / f"{args.scenario_id}__sph"
    terrain_dir = data_dir / args.site / "terrain"
    case_dir = write_case(spec, case_meta, run_dir, terrain_dir)
    print(f"wrote {case_dir}")


if __name__ == "__main__":
    main()

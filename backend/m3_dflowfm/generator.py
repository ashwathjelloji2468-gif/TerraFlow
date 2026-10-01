"""Build relocatable D-Flow FM cases from canonical site and M1/M2 products.

The writer intentionally owns its implementation; it does not depend on the reviewed pilot.
Numerical choices and output fields follow ``docs/m3_spec.md``.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from scipy.ndimage import distance_transform_edt
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, substring
from shapely.ops import unary_union

from backend.m2_breach.hydrograph import hydrograph as m2_hydrograph
from backend.shared.site_config import SiteConfig, default_data_dir, load_site_config

KERNEL = Path.home() / "delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh"
SPINUP_S = 7200.0
DEFAULT_STOP_S = 108000.0


def _xyz_samples(raster: Path, points_x: np.ndarray, points_y: np.ndarray) -> np.ndarray:
    with rasterio.open(raster) as src:
        values = src.read(1).astype(float)
        nodata = src.nodata
        transform = src.transform
        xs = transform.c + (np.arange(src.width) + 0.5) * transform.a
        ys = transform.f + (np.arange(src.height) + 0.5) * transform.e
    # RegularGridInterpolator requires ascending axes.
    if ys[0] > ys[-1]:
        ys, values = ys[::-1], values[::-1, :]
    if xs[0] > xs[-1]:
        xs, values = xs[::-1], values[:, ::-1]
    if nodata is not None:
        values[np.isclose(values, nodata)] = np.nan
    if not np.isfinite(values).all():
        valid = np.isfinite(values)
        nearest = distance_transform_edt(~valid, return_distances=False, return_indices=True)
        values = values[tuple(nearest)]
    interp = RegularGridInterpolator((ys, xs), values, bounds_error=False, fill_value=np.nan)
    result = interp(np.column_stack([points_y, points_x]))
    missing = ~np.isfinite(result)
    if missing.any():
        nearest = RegularGridInterpolator((ys, xs), values, method="nearest", bounds_error=False,
                                          fill_value=None)
        result[missing] = nearest(np.column_stack([points_y[missing], points_x[missing]]))
    if not np.isfinite(result).all():
        raise ValueError(f"M1 raster {raster} has nodata/out-of-extent values at mesh samples")
    return result


def _densified_ring(coords, spacing_m: float) -> list[tuple[float, float]]:
    """Split each polygon boundary segment so MeshKernel sees the requested local scale."""
    if spacing_m <= 0:
        raise ValueError(f"mesh spacing must be positive, got {spacing_m!r}")
    ring = list(coords)
    if len(ring) < 2:
        raise ValueError("polygon ring must contain at least two coordinates")
    dense = [(float(ring[0][0]), float(ring[0][1]))]
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        segments = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / spacing_m)))
        dense.extend((float(x0 + (x1 - x0) * step / segments),
                      float(y0 + (y1 - y0) * step / segments))
                     for step in range(1, segments + 1))
    return dense


def _write_case_files(case_dir: Path, config: SiteConfig, scenario_id: str, params: dict,
                      terrain_dir: Path, hydro_t_s: np.ndarray, hydro_q: np.ndarray,
                      *, stop_s: float = DEFAULT_STOP_S, mesh_spacing_m: float | None = None,
                      map_interval_s: float = 60.0, pilot_export_dir: Path | None = None,
                      hydrograph_includes_base_flow: bool = False) -> dict:
    """Materialize FM inputs from canonical site fields and M1/M2 products."""
    from hydrolib.core.base.models import DiskOnlyFileModel
    from hydrolib.core.dflowfm import ExtModel, FMModel, NetworkModel, XYNModel
    from hydrolib.core.dflowfm.bc.models import ForcingModel, QuantityUnitPair, TimeSeries
    from hydrolib.core.dflowfm.ext.models import Boundary, SourceSink
    from hydrolib.core.dflowfm.inifield.models import IniFieldModel, InitialField, ParameterField
    from hydrolib.core.dflowfm.mdu.models import ExternalForcing, General, Geometry, Numerics, Output, Physics, Time
    from hydrolib.core.dflowfm.net.models import Network
    from hydrolib.core.dflowfm.xyn.models import XYNPoint
    from meshkernel import GeometryList

    case_dir.mkdir(parents=True, exist_ok=True)
    inp, out = case_dir / "inputs", case_dir / "output"
    inp.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    epsg = int(config.crs.utm_epsg.value)
    spacing = float(mesh_spacing_m or config.domains.far_field.grid_resolution.value)
    dam_id = config.domains.far_field.inflow.from_
    dam = next((d for d in config.dams if d.id == dam_id), None)
    if dam is None or dam.breach_location.value is None:
        raise ValueError("far-field inflow must refer to a configured dam with breach_location")
    from pyproj import Transformer
    xsrc, ysrc = Transformer.from_crs(4326, epsg, always_xy=True).transform(*dam.breach_location.value)
    pilot_inputs = None
    outlet_override = None
    if pilot_export_dir is not None:
        from backend.m3_common.loaders import load_pilot_inputs, pilot_mesh_geometry
        pilot_inputs = load_pilot_inputs(pilot_export_dir)
        if epsg != 32645:
            raise ValueError("the retained M3 pilot export is EPSG:32645 and cannot be reused for another CRS")
        polygon, _inflow_edge, outlet_override, _pilot_area = pilot_mesh_geometry(pilot_inputs)
        xsrc, ysrc = pilot_inputs.breach_xy
        poi_frame = None
    else:
        domain = gpd.read_file(terrain_dir / "domain.gpkg")
        if domain.crs.to_epsg() != epsg:
            domain = domain.to_crs(epsg=epsg)
        geom = unary_union(list(domain.geometry)).buffer(0)
        poi_frame = gpd.read_file(terrain_dir / "pois.gpkg")
        if poi_frame.crs.to_epsg() != epsg:
            poi_frame = poi_frame.to_crs(epsg=epsg)
        breach = Point(xsrc, ysrc)
        components = sorted(getattr(geom, "geoms", [geom]), key=lambda part: part.area, reverse=True)
        main = components[0]
        corridors = []
        for part in components[1:]:
            a, b = nearest_points(main, part)
            corridors.append(LineString([a, b]).buffer(max(spacing, 30.0)))
        nearest_breach = nearest_points(geom, breach)[0]
        corridors.append(LineString([breach, nearest_breach]).buffer(max(spacing, 30.0)))
        for point in poi_frame.geometry:
            nearest_poi = nearest_points(geom, point)[0]
            corridors.append(LineString([point, nearest_poi]).buffer(max(spacing, 30.0)))
        geom = unary_union([geom, *corridors]).buffer(0).simplify(spacing * 0.5, preserve_topology=True)
        if geom.geom_type != "Polygon":
            raise ValueError(f"connected M1 domain must be a single polygon after bridge creation; got {geom.geom_type}")
        polygons = list(getattr(geom, "geoms", [geom]))
        xs: list[float] = []
        ys: list[float] = []
        for part in polygons:
            if part.geom_type != "Polygon" or part.area < spacing * spacing / 100:
                continue
            if xs:
                xs.append(-999.0)
                ys.append(-999.0)
            # MeshKernel derives the interior triangle scale from polygon edge lengths.
            # Simplification alone does not impose mesh_spacing_m: long raster-domain edges
            # otherwise become kilometre-scale faces (and leave narrow channels unresolved).
            ring = _densified_ring(part.exterior.coords, spacing)
            xs.extend(x for x, _ in ring)
            ys.extend(y for _, y in ring)
        polygon = GeometryList(x_coordinates=xs, y_coordinates=ys)
    network_model = NetworkModel()
    network_model.network.mesh2d_create_triangular_within_polygon(polygon)
    network_model.network.meshkernel.mesh2d_delete_small_flow_edges_and_small_triangles(0.1, 0.0)
    orthogonality = network_model.network.meshkernel.mesh2d_get_orthogonality().values
    orthogonality = orthogonality[orthogonality != -999.0]
    max_cosphi = float(np.nanmax(orthogonality)) if len(orthogonality) else 0.0
    if max_cosphi > 0.99:
        raise ValueError(f"mesh cos(phi) max {max_cosphi:.6f} exceeds the fixed M3 threshold 0.99")
    mesh = network_model.network._mesh2d.get_mesh2d()
    nx, ny = np.asarray(mesh.node_x), np.asarray(mesh.node_y)
    fx, fy = np.asarray(mesh.face_x), np.asarray(mesh.face_y)
    if pilot_inputs is None:
        bed = _xyz_samples(terrain_dir / "dem.tif", nx, ny)
        rough = _xyz_samples(terrain_dir / "roughness.tif", fx, fy)
    else:
        from backend.m3_common.loaders import interpolate_xy
        bed = interpolate_xy(pilot_inputs.dem_xyv, nx, ny)
        rough = interpolate_xy(pilot_inputs.roughness_xyv, fx, fy)
    if np.any(rough <= 0):
        raise ValueError("M1 Manning values must be positive")
    network_model.network._mesh2d.mesh2d_node_z = bed
    net_path = inp / "domain_net.nc"
    network_model.network.to_file(net_path)
    check = Network.from_file(net_path)._mesh2d.get_mesh2d()
    if (len(nx), len(mesh.edge_x), len(fx)) != (len(check.node_x), len(check.edge_x), len(check.face_x)):
        raise RuntimeError("written D-Flow FM net failed node/edge/face count round-trip")
    if not (np.allclose(nx, check.node_x, atol=1e-6) and np.allclose(ny, check.node_y, atol=1e-6)):
        raise RuntimeError("written D-Flow FM net failed node coordinate round-trip")

    def write_xyz(filename: str, x: np.ndarray, y: np.ndarray, z: np.ndarray, fmt: str) -> None:
        with (inp / filename).open("w") as stream:
            for a, b, c in zip(x, y, z):
                stream.write(f"{a:.6f} {b:.6f} {c:{fmt}}\n")
    write_xyz("bedlevel.xyz", nx, ny, bed, ".6f")
    write_xyz("manning.xyz", fx, fy, rough, ".8f")
    ini = IniFieldModel(
        initial=[InitialField(quantity="bedlevel", datafile=DiskOnlyFileModel(filepath=Path("bedlevel.xyz")),
                              datafiletype="sample", interpolationmethod="triangulation", locationtype="all")],
        parameter=[ParameterField(quantity="frictioncoefficient", datafile=DiskOnlyFileModel(filepath=Path("manning.xyz")),
                                  datafiletype="sample", interpolationmethod="triangulation", locationtype="all")],
    )
    ini.save(filepath=inp / "initial_fields.ini")
    ini = IniFieldModel(filepath=inp / "initial_fields.ini")

    # M2 supplies breach-only discharge; the configured steady flow is added once here.
    base_flow = 0.0 if hydrograph_includes_base_flow else float(config.domains.far_field.inflow.base_flow.value)
    t = np.asarray(hydro_t_s, dtype=float)
    q = np.asarray(hydro_q, dtype=float)
    if t.ndim != 1 or t.shape != q.shape or len(t) < 2 or np.any(np.diff(t) <= 0):
        raise ValueError("M2 hydrograph must contain matching, increasing t_s and q_m3s arrays")
    # D-Flow FM requires the simulation end to align with DtUser (30 s here).
    # M2's hydrograph duration is continuous-valued, so extending exactly to
    # its final sample can otherwise produce a solver error for some scenarios.
    dt_user_s = 30.0
    end = math.ceil(max(float(stop_s), float(t[-1]) + SPINUP_S) / dt_user_s) * dt_user_s
    initial_q = float(q[0]) if hydrograph_includes_base_flow else base_flow
    q_rows = [(0.0, initial_q), (SPINUP_S, initial_q)]
    first = 1 if np.isclose(t[0], 0.0) else 0
    q_rows.extend((float(ts + SPINUP_S), float(discharge if hydrograph_includes_base_flow else base_flow + discharge))
                  for ts, discharge in zip(t[first:], q[first:]))
    if q_rows[-1][0] < end:
        q_rows.append((end, float(q[-1]) if hydrograph_includes_base_flow else base_flow + float(q[-1])))
    time_fmt, discharge_fmt = (".4f", ".4f") if hydrograph_includes_base_flow else (".6f", ".8f")
    (inp / "breach_source.tim").write_text("".join(
        f"{ts/60:{time_fmt}} {discharge:{discharge_fmt}}\n" for ts, discharge in q_rows
    ))

    ext = ExtModel(
        boundary=[Boundary(quantity="neumannbnd", locationfile=DiskOnlyFileModel(filepath=Path("downstream_outlet.pli")),
                           forcingfile=DiskOnlyFileModel(filepath=Path("downstream.bc")))],
        sourcesink=[SourceSink(id="breach_source", name="breach_source", numcoordinates=1,
            xcoordinates=[xsrc], ycoordinates=[ysrc], discharge=DiskOnlyFileModel(filepath=Path("inputs/breach_source.tim")))],
    )
    ext.general.fileversion = "2.01"
    ext.save(filepath=inp / "forcing.ext")

    if pilot_inputs is None:
        points = poi_frame
        if points.crs.to_epsg() != epsg:
            points = points.to_crs(epsg=epsg)
        observation_points = [XYNPoint(x=float(row.geometry.x), y=float(row.geometry.y), n=str(row.poi_id))
                              for row in points.itertuples()]
    else:
        observation_points = [XYNPoint(x=float(x), y=float(y), n=str(name))
                              for name, x, y in pilot_inputs.pois]
    observation = XYNModel(points=observation_points)
    observation.save(filepath=inp / "observations.xyn")

    # Place the open boundary around the lower-elevation end of the M1 channel centreline.
    # The endpoint is snapped to the mesh boundary; a one-cell segment is extracted along it.
    outlet_name = "downstream_outlet"
    if outlet_override is not None:
        outlet_coords = outlet_override
    else:
        centreline_frame = gpd.read_file(terrain_dir / "centreline.gpkg")
        if centreline_frame.crs.to_epsg() != epsg:
            centreline_frame = centreline_frame.to_crs(epsg=epsg)
        centreline = max(centreline_frame.geometry, key=lambda line: line.length)
        end_a, end_b = Point(centreline.coords[0]), Point(centreline.coords[-1])
        endpoint = min((end_a, end_b), key=lambda p: float(_xyz_samples(terrain_dir / "dem.tif",
            np.array([p.x]), np.array([p.y]))[0]))
        outlet_component = min(polygons, key=lambda poly: poly.distance(endpoint))
        perimeter = outlet_component.exterior
        along = perimeter.project(endpoint)
        half = max(spacing / 2, 10.0)
        lo, hi = max(0.0, along - half), min(perimeter.length, along + half)
        outlet_line = substring(perimeter, lo, hi)
        if outlet_line.geom_type != "LineString" or outlet_line.length < 1.0:
            raise ValueError("could not derive a downstream boundary segment from the M1 domain perimeter")
        outlet_coords = list(outlet_line.coords)
    (inp / f"{outlet_name}.pli").write_text(
        f"{outlet_name}\n{len(outlet_coords)} 2\n" +
        "".join(f"{x:.3f} {y:.3f}\n" for x, y in outlet_coords)
    )
    end_min = end / 60.0
    outlet_forcing = TimeSeries(name=f"{outlet_name}_0001", function="timeseries", timeinterpolation="linear",
        quantityunitpair=[QuantityUnitPair(quantity="time", unit="minutes since 2001-01-01 00:00:00"),
                          QuantityUnitPair(quantity="neumannbnd", unit="-")],
        datablock=[[0.0, 0.0], [end_min, 0.0]])
    ForcingModel(forcing=[outlet_forcing]).save(filepath=inp / "downstream.bc")

    mdu = FMModel(general=General(pathsrelativetoparent=True),
        geometry=Geometry(netfile=DiskOnlyFileModel(filepath=Path("inputs/domain_net.nc")), inifieldfile=ini,
                          bedlevtype=3, bedlevuni=float(np.nanmin(bed)), waterlevini=float(np.nanmin(bed))),
        time=Time(refdate="20010101", tstart=0.0, tstop=end, tunit="S", dtuser=dt_user_s, dtmax=dt_user_s, dtinit=0.5),
        physics=Physics(uniffricttype=2), numerics=Numerics(cflmax=0.7),
        external_forcing=ExternalForcing(extforcefilenew=DiskOnlyFileModel(filepath=Path("inputs/forcing.ext"))),
        output=Output(outputdir=Path("output"), obsfile=[DiskOnlyFileModel(filepath=Path("inputs/observations.xyn"))],
            mapinterval=[map_interval_s], hisinterval=[60.0], **{
                **{key: False for key in Output.model_fields if key.startswith("wrimap_") or key.startswith("wrihis_")},
                "wrimap_waterdepth": True, "wrimap_velocity_magnitude": True,
                "wrihis_waterdepth": True, "wrihis_velocity": True, "wrihis_waterlevel_s1": True,
                # Required for contract run_meta.mass_balance_error_pct extraction.
                "wrihis_balance": True,
            }))
    mdu.save(filepath=case_dir / "model.mdu", recurse=False, path_style="unix")
    mdu_path = case_dir / "model.mdu"
    mdu_text = mdu_path.read_text()
    mdu_text = mdu_text.replace(str(inp / "initial_fields.ini"), "inputs/initial_fields.ini")
    mdu_text = re.sub(r"(?im)^(bedLevType\s*=\s*3[^\n]*)$",
        r"\1\ncosphiutrsh                     = 0.99\nremovesmalllinkstrsh            = 0.0", mdu_text, count=1)
    placeholder_label = f"PLACEHOLDER — {scenario_id}, all inputs placeholder"
    if has_placeholder_values(config):
        mdu_text = f"# {placeholder_label}\n" + mdu_text
    mdu_path.write_text(mdu_text)
    has_placeholders = has_placeholder_values(config)
    metadata = {
        "label": placeholder_label if has_placeholders else "",
        "site_id": config.site.id, "scenario_id": scenario_id, "solver": "D-Flow FM",
        "has_placeholders": has_placeholders, "mesh": {
            "node_count": len(nx), "edge_count": len(mesh.edge_x), "face_count": len(fx),
            "max_cosphi": max_cosphi,
            "roundtrip": {"node_count": [len(nx), len(check.node_x)],
                          "edge_count": [len(mesh.edge_x), len(check.edge_x)],
                          "face_count": [len(fx), len(check.face_x)],
                          "node_coordinates_max_abs_error_m": float(max(np.max(np.abs(nx-check.node_x)),
                                                                        np.max(np.abs(ny-check.node_y))))},
        },
        "crs_epsg": epsg, "base_flow_m3s": base_flow,
        "mesh_spacing_m": spacing,
        "hydrograph_includes_base_flow": bool(hydrograph_includes_base_flow),
        "ext_file_version": "2.01", "paths_relative": True,
        "spinup_s": SPINUP_S, "stop_s": end,
        "map_interval_s": map_interval_s, "history_interval_s": 60.0,
        "map_variables": ["mesh2d_waterdepth", "mesh2d_ucmag"],
        "history_variables": ["waterdepth", "velocity", "waterlevel"],
        "changes_from_fm_defaults": [
            {"setting": "PathsRelativeToParent", "default": 0, "new_value": 1,
             "reason": "M3 rule 4: relocatable case paths"},
            {"setting": "RefDate", "default": 20200101, "new_value": 20010101,
             "reason": "time coordinate for hydrograph and forcing series"},
            {"setting": "BedLevUni", "default": -5.0, "new_value": float(np.nanmin(bed)),
             "reason": "positive-up M1 bedlevel fallback"},
            {"setting": "WaterLevIni", "default": 0.0, "new_value": float(np.nanmin(bed)),
             "reason": "initial level at the lowest sampled bed before base-flow spin-up"},
            {"setting": "TStop", "default": 86400.0, "new_value": end,
             "reason": "configured simulation duration including spin-up"},
            {"setting": "DtUser", "default": 300.0, "new_value": 30.0,
             "reason": "fixed pilot timestep"},
            {"setting": "DtInit", "default": 1.0, "new_value": 0.5,
             "reason": "fixed pilot initial timestep"},
            {"setting": "uniffricttype", "default": 1, "new_value": 2,
             "reason": "Manning roughness samples"},
            {"setting": "MapInterval", "default": 1200.0, "new_value": map_interval_s,
             "reason": "M3 arrival threshold sampling at configured map cadence"},
            {"setting": "HisInterval", "default": 300.0, "new_value": 60.0,
             "reason": "POI history resolution"},
            {"setting": "OutputDir", "default": "", "new_value": "output",
             "reason": "case-local outputs"},
            {"setting": "WriMap_*", "default": "FM defaults", "new_value": ["waterdepth", "velocity magnitude"],
             "reason": "M3 rule 5 lean map output"},
            {"setting": "WriHis_*", "default": "FM defaults", "new_value": ["waterdepth", "velocity", "waterlevel"],
             "reason": "POI outputs only"},
            {"setting": "Cosphiutrsh", "default": 0.5, "new_value": 0.99,
             "reason": "fixed pilot mesh-orthogonality threshold"},
            {"setting": "Removesmalllinkstrsh", "default": 0.1, "new_value": 0.0,
             "reason": "fixed pilot; MeshKernel cleanup runs before writing"},
        ],
        "files": ["model.mdu", "inputs/domain_net.nc", "inputs/forcing.ext", "inputs/downstream.bc",
                  "inputs/downstream_outlet.pli", "inputs/breach_source.tim", "inputs/observations.xyn",
                  "inputs/initial_fields.ini", "inputs/bedlevel.xyz", "inputs/manning.xyz"],
        "success_rule": "no ** ERROR in .dia and _map.nc and _his.nc exist",
    }
    (case_dir / "case_meta.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def _walk_sourced(config: SiteConfig):
    """Yield all sourced-value records recursively for placeholder propagation."""
    from pydantic import BaseModel
    from backend.shared.site_config import SourcedValue
    def walk(value):
        if isinstance(value, SourcedValue):
            yield value
        elif isinstance(value, BaseModel):
            for field in type(value).model_fields:
                yield from walk(getattr(value, field))
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)
    yield from walk(config)


def has_placeholder_values(config: SiteConfig) -> bool:
    return any(value.status == "placeholder" for value in _walk_sourced(config))


def placeholder_fields(config: SiteConfig) -> list[str]:
    """Return site-config paths whose values are explicitly marked placeholder."""
    from pydantic import BaseModel
    from backend.shared.site_config import SourcedValue
    found: list[str] = []
    def walk(value, path: str) -> None:
        if isinstance(value, SourcedValue):
            if value.status == "placeholder":
                found.append(path)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name), f"{path}.{name}" if path else name)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
    walk(config, "")
    return found


def build_case(site_id: str, scenario_id: str, params: dict, *, data_dir: str | Path | None = None,
               sites_dir: str | Path | None = None, case_dir: str | Path | None = None,
               stop_s: float = DEFAULT_STOP_S, demo: bool = False) -> tuple[Path, dict]:
    """Generate a case from canonical site config, M1 rasters and an M2 breach scenario."""
    config = load_site_config(site_id, sites_dir=sites_dir)
    if config.domains.far_field.inflow.base_flow is None or config.domains.far_field.inflow.base_flow.value is None:
        raise ValueError("domains.far_field.inflow.base_flow is missing or null")
    dam_id = config.domains.far_field.inflow.from_
    hydro = m2_hydrograph(site_id, dam_id, params, sites_dir=sites_dir)
    # `$SIH26_DATA_DIR`, else `<repo>/data` -- resolved per call, like the rest of the pipeline.
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    terrain = root / site_id / "terrain"
    target = Path(case_dir) if case_dir is not None else root / site_id / "runs" / scenario_id / "dflowfm"
    metadata = _write_case_files(target, config, scenario_id, params, terrain, hydro.t_s, hydro.q_m3s,
        stop_s=stop_s, mesh_spacing_m=90.0 if demo else None, map_interval_s=120.0 if demo else 60.0)
    if demo and metadata["mesh"]["face_count"] > 33018:
        raise ValueError(f"demo mesh has {metadata['mesh']['face_count']} faces; budget is 33018")
    return target, metadata


def build_pilot_reproduction_case(*, case_dir: str | Path, pilot_export_dir: str | Path | None = None,
                                  sites_dir: str | Path | None = None,
                                  stop_s: float = DEFAULT_STOP_S) -> tuple[Path, dict]:
    """Build an FM case from the retained pilot geometry/fields for reproduction audits only.

    This deliberately does not load ``sites/teesta.yaml`` or alter the production generator
    path. It reuses the frozen pilot's own exported geometry, fields, hydrograph, and POIs.
    """
    from backend.m3_common.loaders import load_pilot_inputs

    root = Path(__file__).resolve().parents[2]
    export_dir = Path(pilot_export_dir) if pilot_export_dir else root / "backend/m3_pilot/inputs/export"
    config = load_site_config("teesta_pilot", sites_dir=sites_dir or root / "backend/m3_pilot/inputs")
    inputs = load_pilot_inputs(export_dir)
    hydro = np.asarray(inputs.hydrograph_min_q, dtype=float)
    # Frozen export times are minutes since t0; source Q already includes pilot base flow.
    target = Path(case_dir)
    metadata = _write_case_files(
        target, config, "teesta_pilot_s001_reproduction", {}, root / "data/teesta_pilot/terrain",
        hydro[:, 0] * 60.0, hydro[:, 1], stop_s=stop_s, mesh_spacing_m=90.0,
        map_interval_s=120.0, pilot_export_dir=export_dir, hydrograph_includes_base_flow=True,
    )
    metadata["reproduction_reference"] = "backend/m3_pilot/dflowfm frozen pilot exports"
    metadata["scientific_status"] = "controlled reproduction only; pilot inputs are placeholders"
    (target / "case_meta.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return target, metadata


def write_case(case_dir: str | Path, model: str = "model.mdu", *, kernel: str | Path = KERNEL) -> None:
    """Run FM detached after stripping WSL-mounted Windows directories from PATH."""
    case_dir = Path(case_dir).resolve()
    output = case_dir / "output"
    output.mkdir(exist_ok=True)
    for artifact in output.iterdir():
        if artifact.is_file():
            artifact.unlink()
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(part for part in env.get("PATH", "").split(os.pathsep)
                                    if not re.match(r"^/mnt/", part))
    script = Path(kernel)
    proc = subprocess.Popen([str(script), model], cwd=case_dir, env=env,
                            stdout=(case_dir / "run.stdout.log").open("w"),
                            stderr=(case_dir / "run.stderr.log").open("w"), start_new_session=True)
    (case_dir / "run.pid").write_text(f"{proc.pid}\n")


def check_run_success(case_dir: str | Path, model_stem: str = "model") -> dict:
    """Apply M3 rule 1; the kernel exit status is deliberately not consulted."""
    case_dir = Path(case_dir)
    output = case_dir / "output"
    dia = output / f"{model_stem}.dia"
    errors = []
    if not dia.is_file():
        errors.append(f"missing diagnostic file: {dia}")
    else:
        errors.extend(line for line in dia.read_text(errors="replace").splitlines()
                      if line.startswith("** ERROR"))
    expected = [output / f"{model_stem}_map.nc", output / f"{model_stem}_his.nc"]
    missing = [str(path) for path in expected if not path.is_file()]
    return {"success": not errors and not missing, "dia_errors": errors, "missing_outputs": missing}

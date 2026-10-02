"""M6 impact engine for persisted M5 emulator queries (Feature 9; docs/impact_outputs.md, contract §4.7).

Reads only artifacts a completed `gp_emulator` query already wrote under
`data/<site_id>/queries/<query_id>/` (`backend/m5_emulator/service.py`) plus the site's stored
exposure layers. It never re-runs M5, M3 or M4, and never fills a missing input with an estimate.

- Zones come from the persisted `p_inundation` with `config/impact.yaml` `impact.zone_*_p`
  (not from M5's own `extent_class`, whose HIGH threshold is M5's 0.9). In unknown-breach mode
  that probability combines breach-parameter and emulator uncertainty; in scenario mode it is the
  GP's own (emulator-only) uncertainty, and the impact says so -- the spec's breach-pair rule
  needs two runs that M5 does not make.
- Low-confidence cells are NOT moved down a zone: M5 persists no per-cell confidence, so the
  downgrade cannot be done without inventing one. Stated in `data_coverage_notes`.
- Population needs `exposure/population.tif` on exactly the query's grid; nothing is resampled.
- Loss reuses `loss.estimate_loss` on the persisted depth P10/P50/P90 maps.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import yaml

from backend.shared.grid import FLOAT_NODATA, CanonicalGrid

CONTRACT_VERSION = "0.3.0"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "impact.yaml"
ZONE_NONE, ZONE_POSSIBLE, ZONE_HIGH = 0, 1, 2
ZONE_NAME = {ZONE_HIGH: "high", ZONE_POSSIBLE: "possible"}


# ============================================================================ settings
@dataclass(frozen=True)
class ImpactSettings:
    depth_classes_m: tuple[float, ...]
    depth_class_labels: tuple[str, ...]
    arrival_bands_min: tuple[float, ...]
    arrival_percentile: int
    detection_delay_min: float
    zone_high_p: float
    zone_possible_p: float
    population_sig_figs: int
    population_floor: float
    dv_hazard: dict

    def __post_init__(self):
        if len(self.depth_classes_m) != len(self.depth_class_labels):
            raise ValueError("impact.depth_classes_m and depth_class_labels must have the same length")
        if list(self.depth_classes_m) != sorted(self.depth_classes_m) or list(self.arrival_bands_min) != sorted(self.arrival_bands_min):
            raise ValueError("impact class/band edges must be ascending")
        if not 0 < self.zone_possible_p < self.zone_high_p <= 1:
            raise ValueError("impact zone thresholds need 0 < zone_possible_p < zone_high_p <= 1")

    @property
    def detection_delay_s(self) -> float:
        return self.detection_delay_min * 60.0


def load_impact_settings(path: str | Path = CONFIG_PATH) -> ImpactSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["impact"]
    return ImpactSettings(
        depth_classes_m=tuple(float(v) for v in raw["depth_classes_m"]),
        depth_class_labels=tuple(raw["depth_class_labels"]),
        arrival_bands_min=tuple(float(v) for v in raw["arrival_bands_min"]),
        arrival_percentile=int(raw["arrival_percentile"]), detection_delay_min=float(raw["detection_delay_min"]),
        zone_high_p=float(raw["zone_high_p"]), zone_possible_p=float(raw["zone_possible_p"]),
        population_sig_figs=int(raw["population_sig_figs"]), population_floor=float(raw["population_floor"]),
        dv_hazard=dict(raw.get("dv_hazard_m2s") or {}),
    )


# ============================================================================ classifications
def depth_class_label(depth_m: float | None, settings: ImpactSettings) -> str | None:
    """Highest configured class whose lower edge `depth_m` reaches; None below the first edge."""
    if depth_m is None or not math.isfinite(depth_m):
        return None
    label = None
    for edge, name in zip(settings.depth_classes_m, settings.depth_class_labels):
        if depth_m >= edge:
            label = name
    return label


def depth_class_array(depth: np.ndarray, valid: np.ndarray, settings: ImpactSettings) -> np.ndarray:
    """uint8: 0 = below the first edge / nodata, k = k-th configured class (1-based)."""
    out = np.zeros(depth.shape, dtype=np.uint8)
    for k, edge in enumerate(settings.depth_classes_m, start=1):
        out[valid & (depth >= edge)] = k
    return out


def zone_array(p_inundation: np.ndarray, valid: np.ndarray, settings: ImpactSettings) -> np.ndarray:
    out = np.zeros(p_inundation.shape, dtype=np.uint8)
    out[valid & (p_inundation >= settings.zone_possible_p)] = ZONE_POSSIBLE
    out[valid & (p_inundation >= settings.zone_high_p)] = ZONE_HIGH
    return out


def zone_of(p: float | None, settings: ImpactSettings) -> str | None:
    if p is None or not math.isfinite(p):
        return None
    if p >= settings.zone_high_p:
        return "high"
    if p >= settings.zone_possible_p:
        return "possible"
    return None


def arrival_bands(settings: ImpactSettings) -> list[dict]:
    """[{index, from_s, to_s, label}] with a final open band "> last"."""
    edges_s = [0.0] + [m * 60.0 for m in settings.arrival_bands_min]
    out = [{"index": i + 1, "from_s": a, "to_s": b, "label": f"{a / 60:g}-{b / 60:g} min"}
           for i, (a, b) in enumerate(zip(edges_s, edges_s[1:]))]
    out.append({"index": len(out) + 1, "from_s": edges_s[-1], "to_s": None, "label": f"> {edges_s[-1] / 60:g} min"})
    return out


def arrival_band_array(arrival_s: np.ndarray, valid: np.ndarray, settings: ImpactSettings) -> np.ndarray:
    """uint8: 0 = never arrived / nodata, k = k-th band of `arrival_bands`."""
    out = np.zeros(arrival_s.shape, dtype=np.uint8)
    for band in arrival_bands(settings):
        hi = np.inf if band["to_s"] is None else band["to_s"]
        out[valid & (arrival_s >= band["from_s"]) & (arrival_s < hi)] = band["index"]
    return out


def lead_time(arrival_s: float | None, settings: ImpactSettings) -> tuple[float | None, bool]:
    """Lead time after detection = arrival - detection delay. Product rule: floored at 0 s and
    flagged (`True`) when water arrives before the detection delay has passed -- a negative lead
    time is never shown as a warning window."""
    if arrival_s is None:
        return None, False
    lead = arrival_s - settings.detection_delay_s
    return (0.0, True) if lead < 0 else (lead, False)


def population_display(count: float | None, settings: ImpactSettings) -> str | None:
    """2-significant-figure display string; "< floor" below the floor (docs/impact_outputs.md §3.2)."""
    if count is None:
        return None
    if count < settings.population_floor:
        return f"< {settings.population_floor:g}"
    digits = settings.population_sig_figs - int(math.floor(math.log10(count))) - 1
    return f"{round(count, digits):,.0f}"


# ============================================================================ raster I/O
def _read_on_grid(path: Path, grid: CanonicalGrid) -> tuple[np.ndarray, np.ndarray]:
    with rasterio.open(path) as ds:
        if ds.shape != grid.shape or tuple(ds.transform)[:6] != tuple(grid.transform)[:6]:
            raise ValueError(f"{path.name} is not on the query grid {grid.grid_id} ({ds.shape} vs {grid.shape})")
        array = ds.read(1).astype(np.float64)
        nodata = ds.nodata
    valid = np.isfinite(array) if nodata is None else (np.isfinite(array) & (array != nodata))
    return array, valid


def polygonize(classes: np.ndarray, grid: CanonicalGrid, props_by_class: dict[int, dict]) -> dict:
    """GeoJSON FeatureCollection (EPSG:4326), one MultiPolygon per non-zero class value."""
    import rasterio.features
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as transform_geometry, unary_union

    to_wgs84 = Transformer.from_crs(grid.crs_epsg, 4326, always_xy=True).transform
    features = []
    for value, props in sorted(props_by_class.items()):
        mask = classes == value
        if not mask.any():
            continue
        geoms = [shape(g) for g, v in rasterio.features.shapes(mask.astype(np.uint8), mask=mask,
                                                                 transform=grid.transform) if v == 1]
        features.append({"type": "Feature", "geometry": mapping(transform_geometry(to_wgs84, unary_union(geoms))),
                         "properties": dict(props)})
    return {"type": "FeatureCollection", "features": features}


# ============================================================================ exposure
def _exposure_layer(exposure_dir: Path, name: str, grid: CanonicalGrid):
    import geopandas as gpd

    path = exposure_dir / name
    if not path.is_file():
        return None
    gdf = gpd.read_file(path)
    return gdf.to_crs(epsg=grid.crs_epsg) if gdf.crs is not None else gdf


def _zone_at_points(zones: np.ndarray, grid: CanonicalGrid, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    col = np.floor((xs - grid.origin_x) / grid.cell_size_m).astype(np.int64)
    row = np.floor((grid.origin_y - ys) / grid.cell_size_m).astype(np.int64)
    inside = (row >= 0) & (row < grid.height) & (col >= 0) & (col < grid.width)
    out = np.zeros(len(xs), dtype=np.uint8)
    out[inside] = zones[row[inside], col[inside]]
    return out


def assets_by_zone(exposure_dir: Path, grid: CanonicalGrid, zones: np.ndarray) -> tuple[dict, list[str]]:
    """Contract §4.7 `assets`, HIGH and POSSIBLE as exclusive buckets. Buildings: footprint
    centroid's zone (the M6 building rule); roads: length of line inside each zone's polygons;
    facilities: point's zone. A missing layer is reported, and its counts stay 0 with a note --
    never estimated."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    import rasterio.features

    notes: list[str] = []
    assets = {"buildings": {"high": 0, "possible": 0}, "roads_m": {"high": 0.0, "possible": 0.0},
              "bridges": {"high": 0, "possible": 0}, "hospitals": {"high": 0, "possible": 0},
              "schools": {"high": 0, "possible": 0}, "cropland_m2": {"high": 0.0, "possible": 0.0},
              "hydropower": []}
    buildings = _exposure_layer(exposure_dir, "buildings.gpkg", grid)
    if buildings is None:
        notes.append("buildings.gpkg missing: building exposure not assessed (counts are 0, not measured).")
    elif len(buildings):
        c = buildings.geometry.centroid
        z = _zone_at_points(zones, grid, c.x.to_numpy(), c.y.to_numpy())
        assets["buildings"] = {"high": int((z == ZONE_HIGH).sum()), "possible": int((z == ZONE_POSSIBLE).sum())}
    roads = _exposure_layer(exposure_dir, "roads.gpkg", grid)
    if roads is None:
        notes.append("roads.gpkg missing: road exposure not assessed (lengths are 0, not measured).")
    elif len(roads):
        for value, key in ((ZONE_HIGH, "high"), (ZONE_POSSIBLE, "possible")):
            mask = zones == value
            if mask.any():
                poly = unary_union([shape(g) for g, v in rasterio.features.shapes(
                    mask.astype(np.uint8), mask=mask, transform=grid.transform) if v == 1])
                assets["roads_m"][key] = float(roads.geometry.intersection(poly).length.sum())
    facilities = _exposure_layer(exposure_dir, "facilities.gpkg", grid)
    if facilities is None:
        notes.append("facilities.gpkg missing: bridges/hospitals/schools not assessed (counts are 0, not measured).")
    elif len(facilities):
        z = _zone_at_points(zones, grid, facilities.geometry.x.to_numpy(), facilities.geometry.y.to_numpy())
        kinds = facilities.get("kind")
        for kind_value, key in (("bridge", "bridges"), ("hospital", "hospitals"), ("school", "schools")):
            sel = (kinds.astype(str).str.lower() == kind_value).to_numpy() if kinds is not None else np.zeros(len(z), bool)
            assets[key] = {"high": int(((z == ZONE_HIGH) & sel).sum()), "possible": int(((z == ZONE_POSSIBLE) & sel).sum())}
    notes.append("No cropland or hydropower exposure layer exists; cropland_m2 and hydropower are not assessed.")
    return assets, notes


# ============================================================================ estimates
def _estimate(value, low, high, unit, *, interval="P10-P90", confidence=None, basis=None, source=None) -> dict:
    d = {"value": value, "low": low, "high": high, "unit": unit, "interval": interval, "kind": "predicted"}
    if confidence is not None:
        d["confidence"] = confidence
    if basis is not None:
        d["basis"] = basis
    if source is not None:
        d["source"] = source
    return d


def _cell(array: np.ndarray, valid: np.ndarray, row: int, col: int) -> float | None:
    return float(array[row, col]) if valid[row, col] else None


# ============================================================================ main
def build_m5_impact(site_dir: Path, query_dir: Path, settings: ImpactSettings | None = None) -> dict:
    """`impact.json` (contract §4.7) for a persisted `gp_emulator` query. Also writes
    `depth_classes.geojson` and `isochrones.geojson` next to it. Raises FileNotFoundError /
    ValueError when a required query artifact is missing or off-grid."""
    import geopandas as gpd

    settings = settings or load_impact_settings()
    result = json.loads((query_dir / "result.json").read_text(encoding="utf-8"))
    if result.get("method") != "gp_emulator":
        raise ValueError(f"build_m5_impact needs a gp_emulator query, got {result.get('method')!r}")
    site_id, query_id, mode = result["site_id"], result["query_id"], result["mode"]
    layers_dir = query_dir / "layers"
    grid = CanonicalGrid.from_json(site_dir / "terrain" / "grid.json")
    needed = ["p_inundation", "depth_p10", "depth_p50", "depth_p90", "arrival_p50"]
    missing = [n for n in needed if not (layers_dir / f"{n}.tif").is_file()]
    if missing:
        raise FileNotFoundError(f"M5 query {query_id} is missing persisted layers: {missing}")
    rasters = {}
    for name in ["p_inundation", "depth_p10", "depth_p50", "depth_p90", "arrival_p10", "arrival_p50",
                 "arrival_p90", "velocity_p50", "velocity_p90"]:
        path = layers_dir / f"{name}.tif"
        rasters[name] = _read_on_grid(path, grid) if path.is_file() else None

    p, p_valid = rasters["p_inundation"]
    zones = zone_array(p, p_valid, settings)
    conf = result.get("confidence") or {}
    level = lambda out: (conf.get(out) or {}).get("level")
    exposure_dir = site_dir / "exposure"
    notes: list[str] = []

    # ---- population (exact grid only)
    pop_path = exposure_dir / "population.tif"
    exposure_meta = {}
    if (exposure_dir / "provenance.json").is_file():
        exposure_meta = json.loads((exposure_dir / "provenance.json").read_text())
    pop_meta = exposure_meta.get("population", {}) or {}
    pop_source = f"{pop_meta.get('dataset', 'WorldPop')} {pop_meta.get('year', '')}".strip()
    population_display_block: dict = {}
    if not pop_path.is_file():
        population = _estimate(None, None, None, "persons", interval="none", confidence="LOW",
                               basis="exposure/population.tif missing: population not assessed")
        notes.append("Population not assessed: exposure/population.tif is missing.")
    else:
        try:
            pop, pop_valid = _read_on_grid(pop_path, grid)
        except ValueError as e:
            pop = None
            population = _estimate(None, None, None, "persons", interval="none", confidence="LOW",
                                   basis=f"population.tif not on the query grid; not resampled ({e})")
            notes.append(f"Population not assessed: {e}")
        if pop is not None:
            use = pop_valid & (pop > 0)
            high_n = float(pop[use & (zones == ZONE_HIGH)].sum())
            upper_n = float(pop[use & (zones >= ZONE_POSSIBLE)].sum())
            population = _estimate(upper_n, high_n, upper_n, "persons", interval="zone_range",
                                   confidence=level("extent"), source=pop_source,
                                   basis=("low = population in HIGH cells, high = HIGH + POSSIBLE; value is the "
                                          "upper (HIGH + POSSIBLE) count -- the headline is the low-high range, "
                                          "not a probability-weighted expected count"))
            bands_arr = None
            if rasters["arrival_p50"] is not None:
                arr_name = f"arrival_p{settings.arrival_percentile}"
                arr = rasters.get(arr_name) or rasters["arrival_p50"]
                bands_arr = arrival_band_array(arr[0], arr[1], settings)
            by_band = []
            if bands_arr is not None:
                for band in arrival_bands(settings):
                    sel = use & (bands_arr == band["index"])
                    by_band.append({"band": band["label"], "from_s": band["from_s"], "to_s": band["to_s"],
                                    "high": float(pop[sel & (zones == ZONE_HIGH)].sum()),
                                    "possible": float(pop[sel & (zones == ZONE_POSSIBLE)].sum())})
            population_display_block = {"low": population_display(high_n, settings),
                                        "high": population_display(upper_n, settings),
                                        "sig_figs": settings.population_sig_figs,
                                        "floor": settings.population_floor, "source": pop_source,
                                        "year": pop_meta.get("year"), "by_arrival_band": by_band}
            notes.append(f"Population uses {pop_source or 'the stored population raster'} redistributed onto the "
                         "query grid by sum-preserving resampling; it is not building-level or census data.")

    # ---- assets
    assets, asset_notes = assets_by_zone(exposure_dir, grid, zones)
    notes += asset_notes

    # ---- warning table
    pois_path = site_dir / "terrain" / "pois.gpkg"
    warning_table, not_affected = [], 0
    if not pois_path.is_file():
        notes.append("terrain/pois.gpkg missing: no warning table.")
    else:
        pois = gpd.read_file(pois_path).to_crs(epsg=grid.crs_epsg)
        for poi in pois.to_dict("records"):
            col = int(np.floor((poi["geometry"].x - grid.origin_x) / grid.cell_size_m))
            row = int(np.floor((grid.origin_y - poi["geometry"].y) / grid.cell_size_m))
            if not (0 <= row < grid.height and 0 <= col < grid.width):
                not_affected += 1
                continue
            p_cell = _cell(p, p_valid, row, col)
            zone = zone_of(p_cell, settings)
            if zone is None:
                not_affected += 1
                continue
            val = {k: (_cell(*rasters[k], row, col) if rasters[k] is not None else None) for k in rasters}
            arrival = _estimate(val["arrival_p50"], val["arrival_p10"], val["arrival_p90"], "s",
                                confidence=level("arrival"), basis="M5 arrival P50 (low P10, high P90) at the POI cell")
            lead_v, before_v = lead_time(val["arrival_p50"], settings)
            lead_lo, before_lo = lead_time(val["arrival_p10"], settings)
            lead_hi, _ = lead_time(val["arrival_p90"], settings)
            warning_table.append({
                "poi_id": poi["poi_id"], "name": poi["name"], "kind": poi.get("kind"),
                "chainage_m": float(poi["chainage_m"]) if poi.get("chainage_m") is not None else None,
                "zone": zone, "p_inundation": p_cell, "arrival_s": arrival,
                "lead_time_s": _estimate(lead_v, lead_lo, lead_hi, "s", confidence=level("arrival"),
                                         basis=f"arrival - detection delay {settings.detection_delay_min:g} min "
                                               "(config impact.detection_delay_min), floored at 0 s"),
                "arrives_before_detection": bool(before_v or before_lo),
                "depth_m": _estimate(val["depth_p50"], val["depth_p10"], val["depth_p90"], "m",
                                     confidence=level("depth"), basis="M5 depth P50 (low P10, high P90) at the POI cell"),
                "depth_class": depth_class_label(val["depth_p50"], settings),
                "velocity_ms": _estimate(val["velocity_p50"], None, val["velocity_p90"], "m/s",
                                         confidence=level("velocity"),
                                         basis="M5 velocity P50 (high P90); velocity P10 is not persisted by M5, so low is null"),
            })
        if "chainage_m" not in pois.columns:
            notes.append("pois.gpkg has no chainage_m column.")
    warning_table.sort(key=lambda r: (r["arrival_s"]["value"] is None, r["arrival_s"]["value"] or 0.0))

    # ---- hazard layers: depth classes + isochrones (real polygons from the persisted rasters)
    d50, d50_valid = rasters["depth_p50"]
    depth_fc = polygonize(depth_class_array(d50, d50_valid, settings), grid,
                          {k: {"depth_class": label, "from_m": edge,
                               "to_m": settings.depth_classes_m[k] if k < len(settings.depth_classes_m) else None,
                               "query_id": query_id, "source_layer": "depth_p50"}
                           for k, (edge, label) in enumerate(zip(settings.depth_classes_m, settings.depth_class_labels), start=1)})
    (query_dir / "depth_classes.geojson").write_text(json.dumps(depth_fc), encoding="utf-8")
    zones_fc = polygonize(zones, grid, {ZONE_HIGH: {"zone": "high", "min_p": settings.zone_high_p, "query_id": query_id},
                                        ZONE_POSSIBLE: {"zone": "possible", "min_p": settings.zone_possible_p,
                                                        "max_p": settings.zone_high_p, "query_id": query_id}})
    (query_dir / "impact_zones.geojson").write_text(json.dumps(zones_fc), encoding="utf-8")
    iso_name = f"arrival_p{settings.arrival_percentile}"
    hazard_layers = {"zones": {"path": "impact_zones.geojson", "source_layer": "p_inundation"},
                     "depth_classes": {"path": "depth_classes.geojson", "source_layer": "depth_p50",
                                       "edges_m": list(settings.depth_classes_m),
                                       "labels": list(settings.depth_class_labels)}}
    if rasters.get(iso_name) is not None:
        arr, arr_valid = rasters[iso_name]
        iso_fc = polygonize(arrival_band_array(arr, arr_valid, settings), grid,
                            {b["index"]: {"band": b["label"], "from_s": b["from_s"], "to_s": b["to_s"],
                                          "iso_s": b["to_s"], "percentile": settings.arrival_percentile,
                                          "query_id": query_id, "source_layer": iso_name}
                             for b in arrival_bands(settings)})
        (query_dir / "isochrones.geojson").write_text(json.dumps(iso_fc), encoding="utf-8")
        hazard_layers["isochrones"] = {"path": "isochrones.geojson", "source_layer": iso_name,
                                       "percentile": settings.arrival_percentile,
                                       "bands_min": list(settings.arrival_bands_min),
                                       "detection_delay_min": settings.detection_delay_min}
    else:
        hazard_layers["isochrones"] = {"path": None, "status": "unavailable",
                                       "reason": f"{iso_name}.tif not persisted for this query"}
    hazard_layers["dv_hazard"] = {"path": None, "status": "unavailable",
                                  "reason": "D·V hazard thresholds are unverified (docs/impact_outputs.md §1.1); not produced"}

    # ---- loss (existing JRC pipeline)
    from backend.m6_impact import loss as m6_loss

    loss_needed = ["damage_curves.csv", "asset_values.csv", "buildings.gpkg", "roads.gpkg"]
    loss_missing = [f for f in loss_needed if not (exposure_dir / f).is_file()]
    if loss_missing:
        loss_block = _estimate(None, None, None, "INR", interval="none", confidence="LOW",
                               basis=f"not computed: exposure/ is missing {', '.join(loss_missing)}")
        loss_block.update(by_asset_class={}, assumptions=[f"economic loss unavailable: missing {', '.join(loss_missing)}"])
        loss_placeholders, loss_caveats = ["loss_inr"], []
    else:
        out = m6_loss.estimate_loss(np.where(rasters["depth_p10"][1], rasters["depth_p10"][0], FLOAT_NODATA),
                                    np.where(d50_valid, d50, FLOAT_NODATA),
                                    np.where(rasters["depth_p90"][1], rasters["depth_p90"][0], FLOAT_NODATA),
                                    grid, exposure_dir, depth_confidence=level("depth"))
        loss_block, loss_placeholders, loss_caveats = out["loss_inr"], out["placeholder_fields"], out["caveats"]

    # ---- caveats / placeholders / notes
    if mode == "scenario":
        notes.append("Scenario mode: zones use the GP's own (emulator-only) exceedance probability for one breach "
                     "scenario, not breach-parameter uncertainty; the spec's breach-pair rule needs two runs M5 does not make.")
    notes.append("Low-confidence cells are not moved down a zone: M5 persists no per-cell confidence. Confidence is "
                 f"the query's global per-output level (overall {level('overall')}).")
    if hazard_layers["isochrones"].get("path"):
        notes.append(f"Isochrones use the arrival P{settings.arrival_percentile} raster (early arrival); "
                     f"lead time subtracts a {settings.detection_delay_min:g} min detection delay.")
    notes.append("D·V hazard classes are not produced: their thresholds are unverified.")
    query_placeholders = [f"query.{f}" for f in result.get("placeholder_fields", [])]
    has_placeholders = bool((result.get("flags") or {}).get("has_placeholders")) or bool(loss_placeholders) or bool(query_placeholders)
    caveats = list(result.get("caveats", []))
    caveats += [c for c in loss_caveats if c["id"] not in {x["id"] for x in caveats}]
    caveats.append({"id": "impact_exposure_coverage", "severity": "warning", "text_key": "impact_exposure_coverage"})
    if mode == "scenario":
        caveats.append({"id": "scenario_emulator_probability", "severity": "warning",
                        "text_key": "caveat_scenario_emulator_probability"})
    if (result.get("flags") or {}).get("has_placeholders"):
        caveats.append({"id": "query_placeholders", "severity": "warning", "text_key": "caveat_query_placeholders"})

    src = result.get("provenance") or {}
    params = src.get("parameters") or {}
    source_files = [f"{site_id}/queries/{query_id}/result.json"] + [
        f"{site_id}/queries/{query_id}/layers/{n}.tif" for n, r in rasters.items() if r is not None]
    for f in ("population.tif", "buildings.gpkg", "roads.gpkg", "facilities.gpkg", "damage_curves.csv",
              "asset_values.csv", "provenance.json"):
        if (exposure_dir / f).is_file():
            source_files.append(f"{site_id}/exposure/{f}")
    provenance = {
        "method": "gp_emulator", "contract_version": CONTRACT_VERSION, "impact_method": "m6_impact.build_m5_impact",
        "run_ids": list(src.get("run_ids") or []),
        **({"emulator_manifest": src["emulator_manifest"]} if src.get("emulator_manifest") else {}),
        **({"code_version": src["code_version"]} if src.get("code_version") else {}),
        "data_sources": ["src_031", "src_032", "src_048", "src_049"],
        "parameters": {"query_id": query_id, "site_id": site_id, "mode": mode, "seed": params.get("seed"),
                       "n_samples": params.get("n_samples"), "query_type": params.get("query_type"),
                       "zone_high_p": settings.zone_high_p, "zone_possible_p": settings.zone_possible_p,
                       "arrival_percentile": settings.arrival_percentile,
                       "detection_delay_min": settings.detection_delay_min,
                       "config": "config/impact.yaml (impact, loss)", "m5_provenance": src},
        "exposure_sources": {"population": pop_meta, "population_source_id": "src_049",
                             "osm": {"dataset": "OpenStreetMap contributors", "source_id": "src_048"}},
        "source_files": source_files,
    }
    impact = {
        "contract_version": CONTRACT_VERSION, "query_id": query_id, "site_id": site_id,
        "population_persons": population, "assets": assets, "loss_inr": loss_block,
        "warning_table": warning_table, "not_affected_poi_count": not_affected, "data_coverage_notes": notes,
        "has_placeholders": has_placeholders,
        "placeholder_fields": query_placeholders + list(loss_placeholders), "caveats": caveats,
        "provenance": provenance, "hazard_layers": hazard_layers,
        "zone_thresholds": {"high_p": settings.zone_high_p, "possible_p": settings.zone_possible_p,
                            "probability_basis": "monte_carlo" if mode == "unknown_breach" else "gp_emulator_uncertainty"},
    }
    if population_display_block:
        impact["population_display"] = population_display_block
    return impact

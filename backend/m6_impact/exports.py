"""M6 — export bundle builders for `GET /export/{query_id}` (docs/handoff_contract.md §4.7 "Exports").

Three real, non-mock builders, used by `backend/m0_api/main.py`'s export route whenever a query
has registered result artifacts:

- `build_shapefile_zip` — a zip of real ESRI Shapefile layers (`.shp/.shx/.dbf/.prj/.cpg`),
  not a placeholder README. Field names follow the contract's <=10-character table.
- `extent_geojson_to_kml` / `pois_to_kml_folder` — KML 2.2, styled from `contracts/styles.json`,
  with popup descriptions carrying the real estimates and caveats (not a bare `<name>` shell).
- `build_pdf_report` — a real, multi-section PDF (map, key numbers, impact summary, warning
  table, caveats, provenance, TerraFlow branding), built with matplotlib's PDF backend since no
  dedicated PDF library is in `environment.yml`.

Scope note (CLAUDE.md rule 3: never invent facts): the contract's shapefile layer list also
names `depth_classes` and `isochrones` vector layers. Building those needs raster-to-polygon
extraction against thresholds (`docs/impact_outputs.md` "depth_classes_m"/"arrival_bands_min")
that are still a draft, not yet in any `config/*.yaml` this code is allowed to read. Rather than
hardcode undecided thresholds, this module ships `extent` (+ a single bucketed `dep_class` label
on it, using the depth-class *edges* docs/impact_outputs.md §1 already settled on) and
`pois_warning`, and says so in the bundle's own README rather than silently dropping the layers.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import geopandas as gpd
from shapely.geometry import Point, shape

TERRAFLOW_BRAND = "TerraFlow — GLOF / Dam-Break Decision Support (SIH26 PS-26161)"

def depth_class_label(max_depth_m: float | None) -> str | None:
    """Bucket a maximum depth into the configured depth classes (`config/impact.yaml`
    `impact.depth_classes_m` / `depth_class_labels`, shared with `impact.py`). `None` (no known
    depth) stays `None` rather than a guessed class."""
    from backend.m6_impact.impact import depth_class_label as _label, load_impact_settings

    return _label(max_depth_m, load_impact_settings())


# Human labels for the caveat ids this export route can see today (`backend/m0_api/real_query.py`,
# `backend/m0_api/real_impact.py`). Anything not listed here falls back to its own id, title-cased
# -- so an unknown caveat is still readable, never dropped or guessed at in more detail than its id
# already implies.
CAVEAT_LABELS = {
    "direct_solver_output": "Direct registered solver output — a single deterministic run, not a scientific validation statement.",
    "dem_depression_ponding": "The maximum depth is a single cell in an unconditioned DEM depression, not a hydraulic peak.",
    "clear_water_steep_reach_velocity": "The maximum velocity is clear-water flow on a steep reach; the model is clear-water only.",
    "mvp_reconstructed_forcing": "Inflow forcing for this run is an MVP reconstruction, not an observed hydrograph.",
    "single_run_impact": "Impact is computed from one deterministic run, not a probability ensemble.",
    "impact_exposure_coverage": "Exposure layer coverage (population/buildings/roads) is incomplete or of unknown currency for this area.",
    "placeholder_data": "This result includes placeholder input values; treat affected numbers as provisional.",
    "poi_grid_snapping": "Some points of interest were not flagged wet despite a nearby wet cell — a grid-resolution snapping limit, not necessarily a genuinely unaffected location.",
    "outside_trained_range": "This query falls outside the emulator's trained input range.",
    "clear_water": "The model is clear-water only; Himalayan events are typically debris/sediment-laden.",
    "synthetic_demo": "Synthetic demo data — a software-integration fixture, not a real site.",
}


def caveat_label(caveat: dict[str, Any]) -> str:
    caveat_id = caveat.get("id", "")
    return CAVEAT_LABELS.get(caveat_id, caveat_id.replace("_", " ").capitalize() or "(unlabelled caveat)")


def _dbf_safe(value: Any, limit: int = 254) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def poi_locations_from_site_config(site_config) -> dict[str, tuple[float, float]]:
    """`{poi.id: (lon, lat)}` for every POI in a loaded `SiteConfig` (`backend/shared/site_config.py`).
    Warning-table entries carry a namespaced `poi_id` (`f"{site_id}__poi__{poi.id}"`,
    e.g. `backend/m1_terrain/centreline.py`); callers strip that prefix themselves."""
    return {poi.id: tuple(poi.location.value) for poi in site_config.points_of_interest}


def build_shapefile_zip(
    extent_geojson: dict,
    *,
    site_id: str,
    query_id: str,
    run_ids: list[str],
    method: str,
    zone: str | None,
    confidence_level: str | None,
    has_placeholders: bool,
    caveats: list[dict],
    summary: dict,
    warning_table: list[dict] | None = None,
    poi_locations: dict[str, tuple[float, float]] | None = None,
    extra_layers: dict[str, dict | None] | None = None,
) -> bytes:
    """A real zip of ESRI Shapefile layers: `extent` (the flood extent polygon(s)) and, when POI
    locations are available, `pois_warning` (points, one per `warning_table` entry). CRS is
    EPSG:4326 to match `extent_geojson`'s own CRS (already reprojected upstream, contract §1.6)."""
    caveats_joined = _dbf_safe(";".join(c.get("id", "") for c in caveats))
    run_id_joined = _dbf_safe(";".join(run_ids)) if run_ids else None
    max_depth = (summary.get("max_depth_m") or {}).get("value")
    max_velocity = (summary.get("max_velocity_ms") or {}).get("value")

    extent_rows = []
    for feature in extent_geojson.get("features", []):
        extent_rows.append({
            "geometry": shape(feature["geometry"]),
            "zone": (feature.get("properties") or {}).get("zone") or zone,
            "dep_p50": max_depth,
            "vel_p50": max_velocity,
            "dep_class": depth_class_label(max_depth),
            "conf": confidence_level,
            "method": method,
            "site_id": site_id,
            "query_id": query_id,
            "run_id": run_id_joined,
            "has_ph": int(bool(has_placeholders)),
            "caveats": caveats_joined,
        })

    poi_rows = []
    for entry in warning_table or []:
        base_id = entry["poi_id"].split("__poi__")[-1] if entry.get("poi_id") else None
        location = (poi_locations or {}).get(base_id) if base_id else None
        if location is None:
            continue
        poi_rows.append({
            "geometry": Point(location),
            "poi_id": _dbf_safe(entry.get("poi_id")),
            "name": _dbf_safe(entry.get("name")),
            "kind": entry.get("kind"),
            "chain_m": entry.get("chainage_m"),
            "zone": entry.get("zone"),
            "p_inund": entry.get("p_inundation"),
            "arr_p50": (entry.get("arrival_s") or {}).get("value"),
            "dep_p50": (entry.get("depth_m") or {}).get("value"),
            "vel_p50": (entry.get("velocity_ms") or {}).get("value"),
            "conf": (entry.get("arrival_s") or {}).get("confidence"),
            "site_id": site_id,
            "query_id": query_id,
        })

    buf = io.BytesIO()
    readme_lines = [
        f"{TERRAFLOW_BRAND}",
        f"site_id={site_id} query_id={query_id} method={method}",
        "",
        "Layers in this bundle:",
        "  extent.*        — the flood extent polygon(s) for this query (EPSG:4326).",
    ]
    with TemporaryDirectory(prefix="export-shp-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            if extent_rows:
                extent_gdf = gpd.GeoDataFrame(extent_rows, geometry="geometry", crs="EPSG:4326")
                extent_gdf.to_file(tmp_path / "extent.shp", driver="ESRI Shapefile", index=False)
                for component in sorted(tmp_path.glob("extent.*")):
                    archive.write(component, component.name)
            if poi_rows:
                pois_gdf = gpd.GeoDataFrame(poi_rows, geometry="geometry", crs="EPSG:4326")
                pois_gdf.to_file(tmp_path / "pois_warning.shp", driver="ESRI Shapefile", index=False)
                for component in sorted(tmp_path.glob("pois_warning.*")):
                    archive.write(component, component.name)
                readme_lines.append("  pois_warning.*  — points of interest from the warning table.")
            for layer_name, fc in (extra_layers or {}).items():
                rows = [{"geometry": shape(f["geometry"]),
                         **{k[:10]: _dbf_safe(v) for k, v in (f.get("properties") or {}).items()}}
                        for f in (fc or {}).get("features", [])]
                if rows:
                    gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_file(
                        tmp_path / f"{layer_name}.shp", driver="ESRI Shapefile", index=False)
                    for component in sorted(tmp_path.glob(f"{layer_name}.*")):
                        archive.write(component, component.name)
                    readme_lines.append(f"  {layer_name}.*  — derived from this query's persisted rasters "
                                        "(config/impact.yaml thresholds).")
                else:
                    readme_lines.append(f"  {layer_name}: not included — no features for this query.")
            if not extra_layers:
                readme_lines += [
                    "",
                    "Not included: depth_classes / isochrones vector layers -- only built for emulator "
                    "(M5) impact results, from their persisted rasters.",
                ]
            archive.writestr("README.txt", "\n".join(readme_lines) + "\n")
    return buf.getvalue()


def _ring_to_kml_coordinates(ring: list) -> str:
    return " ".join(f"{p[0]},{p[1]},0" for p in ring)


def _polygon_to_kml(coordinates: list) -> str:
    """`coordinates` is a GeoJSON Polygon's ring list: `coordinates[0]` is the outer ring,
    any further rings are holes."""
    outer = f"<outerBoundaryIs><LinearRing><coordinates>{_ring_to_kml_coordinates(coordinates[0])}</coordinates></LinearRing></outerBoundaryIs>"
    inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_ring_to_kml_coordinates(ring)}</coordinates></LinearRing></innerBoundaryIs>"
                    for ring in coordinates[1:])
    return f"<Polygon>{outer}{inner}</Polygon>"


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def extent_geojson_to_kml(
    extent_geojson: dict,
    name: str,
    *,
    fill_hex: str = "#f4a259",
    opacity: float = 0.35,
    description: str | None = None,
) -> str:
    """KML 2.2, one Folder holding every part of the (possibly multi-part, possibly holed)
    extent, styled from `contracts/styles.json` (`extent_class.possible` by default)."""
    polygons = []
    for feature in extent_geojson.get("features", []):
        geometry = feature["geometry"]
        if geometry["type"] == "Polygon":
            polygons.append(_polygon_to_kml(geometry["coordinates"]))
        elif geometry["type"] == "MultiPolygon":
            polygons.extend(_polygon_to_kml(part) for part in geometry["coordinates"])
        else:
            raise ValueError(f"unsupported extent geometry type for KML export: {geometry['type']!r}")

    style_id = "extentStyle"
    kml_color_channels = fill_hex.lstrip("#")
    # KML colour order is aabbggrr (alpha, blue, green, red), not the web's rrggbb.
    r, g, b = kml_color_channels[0:2], kml_color_channels[2:4], kml_color_channels[4:6]
    kml_color = f"{int(opacity * 255):02x}{b}{g}{r}"
    style = (f'<Style id="{style_id}"><LineStyle><color>ff{b}{g}{r}</color><width>2</width></LineStyle>'
             f'<PolyStyle><color>{kml_color}</color></PolyStyle></Style>')

    description_xml = f"<description><![CDATA[{description}]]></description>" if description else ""
    if not polygons:
        placemark = ""
    elif len(polygons) == 1:
        placemark = f'<Placemark><name>Extent</name>{description_xml}<styleUrl>#{style_id}</styleUrl>{polygons[0]}</Placemark>'
    else:
        placemark = (f'<Placemark><name>Extent</name>{description_xml}<styleUrl>#{style_id}</styleUrl>'
                     f'<MultiGeometry>{"".join(polygons)}</MultiGeometry></Placemark>')

    return (f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{_xml_escape(name)}</name>'
            f'{style}<Folder><name>Flood extent</name>{placemark}</Folder></Document></kml>')


def pois_kml_folder(warning_table: list[dict], poi_locations: dict[str, tuple[float, float]]) -> str:
    """A `<Folder>` of `<Placemark>` points, one per warning-table POI with a known location,
    each with a popup description carrying its real estimates. Returns `""` when there are none,
    so the caller can skip an empty folder rather than emit an empty shell."""
    placemarks = []
    for entry in warning_table:
        base_id = entry["poi_id"].split("__poi__")[-1] if entry.get("poi_id") else None
        location = poi_locations.get(base_id) if base_id else None
        if location is None:
            continue
        arrival = entry.get("arrival_s") or {}
        depth = entry.get("depth_m") or {}
        velocity = entry.get("velocity_ms") or {}
        description = (
            f"Zone: {entry.get('zone')}<br/>P(inundation): {entry.get('p_inundation')}<br/>"
            f"Arrival: {arrival.get('value')} {arrival.get('unit')}<br/>"
            f"Depth: {depth.get('value')} {depth.get('unit')}<br/>"
            f"Velocity: {velocity.get('value')} {velocity.get('unit')}<br/>"
            f"Confidence: {arrival.get('confidence')}"
        )
        placemarks.append(
            f'<Placemark><name>{_xml_escape(entry.get("name", base_id))}</name>'
            f'<description><![CDATA[{description}]]></description>'
            f'<Point><coordinates>{location[0]},{location[1]},0</coordinates></Point></Placemark>'
        )
    if not placemarks:
        return ""
    return f'<Folder><name>Points of interest</name>{"".join(placemarks)}</Folder>'


def _fmt_estimate(estimate: dict | None) -> str:
    if not estimate or estimate.get("value") is None:
        return "not available"
    value, unit = estimate["value"], estimate.get("unit") or ""
    text = f"{value:,.2f} {unit}".strip() if isinstance(value, float) else f"{value} {unit}".strip()
    low, high = estimate.get("low"), estimate.get("high")
    if low is not None and high is not None and (low, high) != (value, value):
        text += f" ({low:,.2f}–{high:,.2f})"
    return text


def build_pdf_report(
    *,
    site_id: str,
    site_name: str,
    query_id: str,
    report_label: str,
    is_synthetic: bool,
    summary: dict,
    impact: dict | None,
    caveats: list[dict],
    provenance: dict,
    has_placeholders: bool,
    map_png_bytes: bytes | None,
    map_bounds_latlng: list[list[float]] | None,
    map_label: str | None,
) -> bytes:
    """A real, multi-section PDF report — header, map, key numbers, impact summary, warning
    table, caveats, provenance, TerraFlow branding (contract §4.7 `.pdf` row) — built with
    matplotlib's PDF backend (no dedicated PDF library is in `environment.yml`)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    buf = io.BytesIO()
    fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
    fig.suptitle(TERRAFLOW_BRAND, fontsize=9, color="#2c7da0", x=0.05, ha="left", y=0.99)
    fig.text(0.05, 0.965, site_name, fontsize=13, weight="bold", wrap=True)
    fig.text(0.05, 0.947, f"query {query_id}", fontsize=8, family="monospace", color="#555555")
    fig.text(0.05, 0.932, f"{report_label}  ·  generated {generated_at}", fontsize=8, color="#444444")

    grid = fig.add_gridspec(nrows=1, ncols=1, left=0.05, right=0.55, top=0.89, bottom=0.60)
    map_ax = fig.add_subplot(grid[0, 0])
    if map_png_bytes and map_bounds_latlng:
        import numpy as np
        from PIL import Image
        image = Image.open(io.BytesIO(map_png_bytes))
        (west, east), (south, north) = map_bounds_latlng[0], map_bounds_latlng[1]
        map_ax.imshow(np.asarray(image), extent=(west, east, south, north), origin="upper")
        map_ax.set_title(map_label or "Flood extent", fontsize=9)
        map_ax.set_xlabel("lon", fontsize=7)
        map_ax.set_ylabel("lat", fontsize=7)
        map_ax.tick_params(labelsize=6)
    else:
        map_ax.text(0.5, 0.5, "No raster layer available for this query", ha="center", va="center", wrap=True)
        map_ax.set_axis_off()

    numbers_ax = fig.add_axes((0.60, 0.60, 0.35, 0.28))
    numbers_ax.set_axis_off()
    numbers_ax.set_title("Key numbers", fontsize=10, loc="left")
    first_arrival = summary.get("first_arrival") or {}
    rows = [
        ("Maximum depth", _fmt_estimate(summary.get("max_depth_m"))),
        ("Maximum velocity", _fmt_estimate(summary.get("max_velocity_ms"))),
        ("Inundated area", _fmt_estimate(summary.get("inundated_area_m2"))),
        ("Peak discharge", _fmt_estimate(summary.get("peak_discharge_m3s"))),
        (f"First arrival ({first_arrival.get('name', 'n/a')})", _fmt_estimate(first_arrival.get("arrival_s"))),
    ]
    numbers_ax.table(cellText=rows, colWidths=[0.6, 0.4], loc="upper left", cellLoc="left", edges="horizontal")

    body_ax = fig.add_axes((0.05, 0.08, 0.90, 0.50))
    body_ax.set_axis_off()
    lines: list[str] = []
    lines.append("Impact summary")
    if impact:
        population = impact.get("population_persons") or {}
        assets = impact.get("assets") or {}
        loss = impact.get("loss_inr") or {}
        lines.append(f"  Population in flood path: {_fmt_estimate(population)}")
        display = impact.get("population_display") or {}
        if display:
            lines.append(f"    about {display.get('low')}–{display.get('high')} people (HIGH – HIGH+POSSIBLE; "
                         f"{display.get('source') or 'population raster'})")
        zt = impact.get("zone_thresholds") or {}
        if zt:
            lines.append(f"  Zones: HIGH p>={zt.get('high_p')}, POSSIBLE p>={zt.get('possible_p')} "
                         f"({zt.get('probability_basis')})")
        for asset_name, counts in (assets or {}).items():
            if isinstance(counts, dict):
                lines.append(f"  {asset_name}: HIGH={counts.get('high')}  POSSIBLE={counts.get('possible')}")
        lines.append(f"  Estimated loss: {_fmt_estimate(loss)}")
        if loss.get("basis"):
            lines.append(f"    ({loss['basis']})")
        lines.append("")
        lines.append("Warning table (points of interest)")
        for entry in (impact.get("warning_table") or [])[:10]:
            arrival = entry.get("arrival_s") or {}
            depth = entry.get("depth_m") or {}
            lead = entry.get("lead_time_s")
            lines.append(
                f"  {entry.get('name')} [{entry.get('zone')}] — arrival {_fmt_estimate(arrival)}, "
                f"depth {_fmt_estimate(depth)}"
                + (f" ({entry['depth_class']})" if entry.get("depth_class") else "")
                + (f", lead time {_fmt_estimate(lead)}" if lead else "")
            )
        if not impact.get("warning_table"):
            lines.append("  (no points of interest in a HIGH/POSSIBLE zone for this run)")
    else:
        lines.append("  not available for this query")
    lines.append("")
    lines.append("Caveats")
    for caveat in caveats:
        lines.append(f"  • [{caveat.get('severity', 'warning').upper()}] {caveat_label(caveat)}")
    if not caveats:
        lines.append("  (none recorded)")
    lines.append("")
    lines.append("Provenance")
    lines.append(f"  method: {provenance.get('method')}")
    for key in ("scenario_id", "code_version", "input_forcing_note"):  # Feature 12
        if provenance.get(key):
            lines.append(f"  {key}: {provenance[key]}")
    if provenance.get("run_ids"):
        lines.append(f"  run_ids: {', '.join(provenance['run_ids'])}")
    if provenance.get("data_sources"):
        lines.append(f"  data_sources: {', '.join(str(s) for s in provenance['data_sources'])}")
    lines.append(f"  has_placeholders: {has_placeholders}")
    lines.append("")
    lines.append(
        "Software integration fixture only; not a scientific forecast." if is_synthetic else
        "Values are direct registered solver outputs; this is not a scientific validation statement."
    )
    body_ax.text(0, 1, "\n".join(lines), fontsize=8, va="top", ha="left", family="monospace", wrap=True)

    fig.text(0.05, 0.02, TERRAFLOW_BRAND + " — localhost decision-support tool, not an authoritative warning system.",
              fontsize=6, color="#888888")

    with PdfPages(buf) as pdf:
        pdf.savefig(fig)
    plt.close(fig)
    return buf.getvalue()

"""Feature 9: GET /impact/{query_id} and exports for a persisted M5 (gp_emulator) query.

The query is produced by the Feature 7 test emulator (fitted in-test on M5's synthetic library,
saved to the temporary data dir) through the real POST /flood/query path; exposure layers are
small synthetic files written here. Nothing here is a real Teesta model or real exposure."""
from __future__ import annotations

import csv
import io
import json
import zipfile

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import LineString, Point, box

from backend.m0_api import schemas
from backend.m5_emulator import synthetic as sw
from backend.m6_impact import impact as m6_impact
from backend.m6_impact import loss as m6_loss
from backend.shared.grid import write_grid_raster
from tests.m0_api.test_m5_query import API, SITE, _exact, client, synthetic_emulator, trained_site  # noqa: F401

S = m6_impact.load_impact_settings()


def _write_exposure(site_dir, grid):
    exp = site_dir / "exposure"
    exp.mkdir(parents=True, exist_ok=True)
    write_grid_raster(exp / "population.tif", np.full(grid.shape, 10.0, np.float32), grid)
    (exp / "provenance.json").write_text(json.dumps({"population": {"dataset": "WorldPop (test raster)", "year": 2020}}))
    cx = lambda c: grid.origin_x + (c + 0.5) * grid.cell_size_m
    cy = lambda r: grid.origin_y - (r + 0.5) * grid.cell_size_m
    mid = grid.height // 2
    crs = f"EPSG:{grid.crs_epsg}"
    half = grid.cell_size_m / 4
    gpd.GeoDataFrame({"kind": ["house"] * 6 + ["office"]},
                     geometry=[box(cx(c) - half, cy(mid) - half, cx(c) + half, cy(mid) + half) for c in range(0, 30, 5)]
                     + [box(cx(0) - half, cy(0) - half, cx(0) + half, cy(0) + half)], crs=crs).to_file(exp / "buildings.gpkg")
    gpd.GeoDataFrame({"kind": ["track"]}, geometry=[LineString([(cx(0), cy(mid)), (cx(grid.width - 1), cy(mid))])],
                     crs=crs).to_file(exp / "roads.gpkg")
    gpd.GeoDataFrame({"kind": ["hospital", "school"]}, geometry=[Point(cx(5), cy(mid)), Point(cx(0), cy(0))],
                     crs=crs).to_file(exp / "facilities.gpkg")
    curves = [("residential", 0.0, 0.0), ("residential", 6.0, 1.0), ("commercial", 0.0, 0.0), ("commercial", 6.0, 1.0),
              ("industrial", 0.0, 0.0), ("industrial", 6.0, 1.0), ("infrastructure_roads", 0.0, 0.0),
              ("infrastructure_roads", 6.0, 1.0)]
    with open(exp / "damage_curves.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asset_class", "depth_m", "damage_fraction", "source"])
        w.writerows([(c, d, v, "src_031") for c, d, v in curves])
    cfg = m6_loss.load_loss_config()
    fx, idx = cfg.eur_to_inr_2010.value, cfg.price_index_2010_to_current.value
    with open(exp / "asset_values.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asset_class", "value_inr_per_unit", "unit", "source", "status", "value_eur2010", "jrc_cell"])
        for cls, eur in {"residential": 212.78, "commercial": 323.98, "industrial": 293.10, "infrastructure_roads": 2.963}.items():
            w.writerow([cls, eur * fx * idx, "INR/m2", "src_032", "sourced", eur, "test"])


@pytest.fixture
def m5_query(data_dir, trained_site, synthetic_emulator):
    grid = synthetic_emulator.grid
    site = data_dir / SITE
    grid.to_json(site / "terrain" / "grid.json")
    pois = gpd.read_file(site / "terrain" / "pois.gpkg")
    pois["chainage_m"] = [float(sw.SYNTHETIC_POIS[n]) for n in pois["name"]]
    far = {"poi_id": f"{SITE}__poi__far_hill", "name": "far_hill", "kind": "village", "chainage_m": 0.0,
           "geometry": Point(grid.origin_x + 0.5 * grid.cell_size_m, grid.origin_y - 0.5 * grid.cell_size_m)}
    gpd.GeoDataFrame([*pois.to_dict("records"), far], crs=pois.crs).to_file(site / "terrain" / "pois.gpkg")
    _write_exposure(site, grid)
    body = {"site_id": SITE, "model": "delft3d", "mode": "unknown_breach", "inputs": {},
            "options": {"n_samples": 150, "seed": 5}}
    r = client.post(f"{API}/flood/query", json=body)
    assert r.status_code == 200, r.text
    return r.json()["query_id"], site / "queries" / r.json()["query_id"], grid


def _read(path):
    with rasterio.open(path) as ds:
        return ds.read(1)


def test_m5_impact_from_persisted_artifacts(m5_query, monkeypatch):
    query_id, qdir, grid = m5_query
    seen = {}
    real_loss = m6_loss.estimate_loss

    def spy(d10, d50, d90, *args, **kwargs):
        seen.update(d10=d10, d50=d50, d90=d90)
        return real_loss(d10, d50, d90, *args, **kwargs)

    monkeypatch.setattr(m6_loss, "estimate_loss", spy)
    r = client.get(f"{API}/impact/{query_id}")
    assert r.status_code == 200, r.text
    impact = r.json()
    schemas.validate("impact.schema.json", impact)
    assert impact["provenance"]["method"] == "gp_emulator" and (qdir / "impact.json").is_file()

    # Zones from the persisted p_inundation with config thresholds.
    p = _read(qdir / "layers" / "p_inundation.tif")
    zones = m6_impact.zone_array(p, np.isfinite(p) & (p != -9999.0), S)
    pop = impact["population_persons"]
    assert pop["low"] == pytest.approx(10.0 * (zones == 2).sum())
    assert pop["high"] == pytest.approx(10.0 * (zones >= 1).sum())
    assert pop["low"] <= pop["value"] <= pop["high"] and pop["interval"] == "zone_range"
    assert impact["population_display"]["source"] == "WorldPop (test raster) 2020"
    assert impact["population_display"]["year"] == 2020

    # Warning table: affected POIs only, sorted by arrival, ordered quantiles, lead time.
    rows = impact["warning_table"]
    assert rows and all(r["zone"] in ("high", "possible") for r in rows)
    assert "far_hill" not in {r["name"] for r in rows}
    n_pois = len(gpd.read_file(qdir.parents[1] / "terrain" / "pois.gpkg"))
    assert impact["not_affected_poi_count"] == n_pois - len(rows)
    arrivals = [r["arrival_s"]["value"] for r in rows if r["arrival_s"]["value"] is not None]
    assert arrivals == sorted(arrivals)
    for row in rows:
        a = row["arrival_s"]
        if None not in (a["low"], a["value"], a["high"]):
            assert a["low"] <= a["value"] <= a["high"]  # P10 (early) not later than median
        if a["value"] is not None:
            assert row["lead_time_s"]["value"] == pytest.approx(max(a["value"] - S.detection_delay_s, 0.0))
        d = row["depth_m"]
        assert d["low"] <= d["value"] <= d["high"]
        assert row["depth_class"] == m6_impact.depth_class_label(d["value"], S)
        assert row["velocity_ms"]["low"] is None  # M5 persists no velocity P10

    # Loss: existing JRC pipeline fed the persisted depth P10/P50/P90 maps.
    for band in ("10", "50", "90"):
        persisted = _read(qdir / "layers" / f"depth_p{band}.tif")
        np.testing.assert_array_equal(seen[f"d{band}"], np.where(persisted != -9999.0, persisted, -9999.0))
    loss = impact["loss_inr"]
    assert loss["value"] is not None and loss["low"] <= loss["value"] <= loss["high"]
    assert any("wider than the true P10-P90" in a for a in loss["assumptions"])

    # Hazard layers are real polygons written next to impact.json.
    iso = json.loads((qdir / "isochrones.geojson").read_text())
    depth_fc = json.loads((qdir / "depth_classes.geojson").read_text())
    assert iso["features"] and all(f["properties"]["percentile"] == 10 for f in iso["features"])
    assert {f["properties"]["depth_class"] for f in depth_fc["features"]} <= set(S.depth_class_labels)
    assert impact["hazard_layers"]["dv_hazard"]["status"] == "unavailable"
    # Placeholders propagate (teesta site config is placeholder-marked).
    assert impact["has_placeholders"] is True and any(f.startswith("query.") for f in impact["placeholder_fields"])
    assert any("not moved down a zone" in n for n in impact["data_coverage_notes"])


def test_missing_exposure_is_reported_not_estimated(m5_query):
    query_id, qdir, _ = m5_query
    import shutil
    shutil.rmtree(qdir.parents[1] / "exposure")
    impact = client.get(f"{API}/impact/{query_id}").json()
    schemas.validate("impact.schema.json", impact)
    assert impact["population_persons"]["value"] is None and impact["loss_inr"]["value"] is None
    notes = " ".join(impact["data_coverage_notes"])
    for word in ("population.tif is missing", "buildings.gpkg missing", "roads.gpkg missing", "facilities.gpkg missing"):
        assert word in notes
    assert impact["assets"]["buildings"] == {"high": 0, "possible": 0}


def test_m5_exports_carry_real_zone_and_hazard_layers(m5_query):
    query_id, qdir, _ = m5_query
    shp = client.get(f"{API}/export/{query_id}", params={"format": "shp"})
    assert shp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(shp.content)).namelist()
    assert {"extent.shp", "depth_classes.shp", "isochrones.shp", "pois_warning.shp"} <= set(names)
    with zipfile.ZipFile(io.BytesIO(shp.content)) as z:
        z.extractall(qdir / "_shp")
    zones = set(gpd.read_file(qdir / "_shp" / "extent.shp")["zone"])
    assert zones and zones <= {"high", "possible"}
    kml = client.get(f"{API}/export/{query_id}", params={"format": "kml"}).text
    assert "zone</name>" in kml and ("HIGH zone" in kml or "POSSIBLE zone" in kml)
    gj = client.get(f"{API}/export/{query_id}", params={"format": "geojson"}).json()
    layers = {f["properties"]["layer"] for f in gj["features"]}
    assert {"zone", "isochrones", "depth_classes"} <= layers
    pdf = client.get(f"{API}/export/{query_id}", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_sph_direct_impact_is_explicitly_unavailable(data_dir):
    qdir = data_dir / SITE / "queries" / "q_20261002T000000Z_abcdef"
    qdir.mkdir(parents=True)
    (qdir / "result.json").write_text(json.dumps({"method": "sph_direct"}))
    r = client.get(f"{API}/impact/q_20261002T000000Z_abcdef")
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "impact_unavailable"

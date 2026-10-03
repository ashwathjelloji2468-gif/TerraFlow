"""Tests for backend.m6_impact.exposure_worldpop — clip, sum-preserving resample
onto the far-field grid, and skip-existing behaviour. No real network access:
the "national" raster is a small synthetic GeoTIFF built locally."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from affine import Affine

from backend.m6_impact import exposure_worldpop as wp
from backend.shared.grid import build_farfield_grid

# Covers the synth fixture's far-field bbox [88.45, 27.45, 88.55, 27.55] with margin,
# at a coarse ~0.01 deg ("~1km") resolution like the real WorldPop 1km product.
NATIONAL_BOUNDS = (87.0, 26.0, 90.0, 29.0)
NATIONAL_RES_DEG = 0.01
PERSONS_PER_CELL = 50.0


def _make_national_raster(path) -> None:
    west, south, east, north = NATIONAL_BOUNDS
    width = round((east - west) / NATIONAL_RES_DEG)
    height = round((north - south) / NATIONAL_RES_DEG)
    transform = Affine(NATIONAL_RES_DEG, 0.0, west, 0.0, -NATIONAL_RES_DEG, north)
    data = np.full((height, width), PERSONS_PER_CELL, dtype=np.float32)
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=1,
        dtype=data.dtype, crs="EPSG:4326", transform=transform, nodata=-99999.0,
    ) as dst:
        dst.write(data, 1)


@pytest.fixture
def national_raster(tmp_path):
    path = tmp_path / "national.tif"
    _make_national_raster(path)
    return path


# --------------------------------------------------------------------------- pure helpers


def test_worldpop_url_shape():
    url = wp.worldpop_url("IND", 2020)
    assert url.startswith("https://data.worldpop.org/GIS/Population/")
    assert "IND/ind_ppp_2020_1km_Aggregated_UNadj.tif" in url


def test_clip_to_bbox_matches_requested_extent(tmp_path, national_raster):
    bbox = [88.45, 27.45, 88.55, 27.55]
    out_path = tmp_path / "clip.tif"
    wp.clip_to_bbox(national_raster, bbox, out_path)

    with rasterio.open(out_path) as ds:
        left, bottom, right, top = ds.bounds
        assert left <= bbox[0] and bottom <= bbox[1]
        assert right >= bbox[2] and top >= bbox[3]
        assert ds.crs.to_epsg() == 4326
        data = ds.read(1, masked=True)
        assert np.ma.filled(data, 0).max() == pytest.approx(PERSONS_PER_CELL)


def test_resample_sum_preserving_keeps_total_close(tmp_path, national_raster, synth_config):
    bbox = synth_config.domains.far_field.bbox.value
    clip_path = tmp_path / "clip.tif"
    wp.clip_to_bbox(national_raster, bbox, clip_path)

    out_path = tmp_path / "population.tif"
    _, source_sum, resampled_sum = wp.resample_sum_preserving(clip_path, synth_config, out_path)

    assert source_sum > 0
    assert resampled_sum == pytest.approx(source_sum, rel=0.1)

    grid = build_farfield_grid(synth_config)
    with rasterio.open(out_path) as ds:
        assert (ds.height, ds.width) == grid.shape
        assert ds.crs.to_epsg() == grid.crs_epsg


# --------------------------------------------------------------------------- fetch()


def test_fetch_rejects_placeholder_bbox(synth_config):
    cfg = synth_config.model_copy(deep=True)
    cfg.domains.far_field.bbox.value = None
    cfg.domains.far_field.bbox.status = "placeholder"
    with pytest.raises(ValueError, match="placeholder"):
        wp.fetch(cfg, data_dir=None)


def test_fetch_downloads_clips_resamples_and_writes_provenance(tmp_path, national_raster, synth_config, monkeypatch):
    monkeypatch.setattr(wp, "download_national_raster", lambda *a, **k: national_raster)

    entry = wp.fetch(synth_config, data_dir=tmp_path, iso3="IND")  # Feature 16: explicit, no default

    assert entry["status"] == "fetched"
    exposure_path = tmp_path / synth_config.site.id / "exposure" / "population.tif"
    assert exposure_path.exists()
    raw_clip = tmp_path / synth_config.site.id / "raw" / f"worldpop_ind_ppp_{wp.WORLDPOP_YEAR}_1km_unadj_clip.tif"
    assert raw_clip.exists()

    provenance = (exposure_path.parent / "provenance.json").read_text(encoding="utf-8")
    assert "population" in provenance
    assert entry["license"] == wp.WORLDPOP_LICENSE


def test_fetch_skips_when_population_tif_already_exists(tmp_path, synth_config, monkeypatch):
    exposure_dir = tmp_path / synth_config.site.id / "exposure"
    exposure_dir.mkdir(parents=True)
    (exposure_dir / "population.tif").write_text("placeholder")

    def _boom(*args, **kwargs):
        raise AssertionError("should not download when population.tif already exists")

    monkeypatch.setattr(wp, "download_national_raster", _boom)
    entry = wp.fetch(synth_config, data_dir=tmp_path)
    assert entry == {"file": "population.tif", "status": "skipped_existing"}

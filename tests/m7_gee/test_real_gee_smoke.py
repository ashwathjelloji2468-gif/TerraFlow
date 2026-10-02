"""Feature 10 manual smoke test against REAL Earth Engine (Teesta, 2 months). Skipped unless
`TERRAFLOW_GEE_SMOKE=1`, `earthengine-api` is installed and EE initialises (OAuth via
`earthengine authenticate` + `GEE_PROJECT`, or the service-account variables in `.env`).
Writes only into a temporary directory; prints no credential material.

Run: `TERRAFLOW_GEE_SMOKE=1 GEE_PROJECT=<project> pytest -q -s tests/m7_gee/test_real_gee_smoke.py`
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TERRAFLOW_GEE_SMOKE") != "1",
                                reason="set TERRAFLOW_GEE_SMOKE=1 (and EE credentials) for the real-GEE smoke test")


def test_real_teesta_fetch_reaches_earth_engine(tmp_path):
    pytest.importorskip("ee")
    from backend.m0_api import schemas
    from backend.m7_gee import cache, fetch
    from backend.m7_gee.provider import EarthEngineProvider
    from backend.m7_gee.scene_search import _ee_initialize
    from backend.m7_gee.settings import GeeSettings

    try:
        _ee_initialize(os.environ.get("GEE_PROJECT"))
    except RuntimeError as e:
        pytest.skip(f"Earth Engine not initialised: {cache.safe_error(e)}")
    result = fetch.run("teesta", settings=GeeSettings(months_back=2, rain_days_back=10),
                       provider=EarthEngineProvider(), data_dir=tmp_path)
    layers = cache.load_layers("teesta", tmp_path)
    schemas.validate("gee_layers.schema.json", layers)
    print("products:", result.products, "errors:", result.errors)
    print("lake rows:", layers["lake_area_series"])
    print("rainfall days:", len(layers["rainfall"]), "accumulations:", layers["rainfall_accumulations"])
    assert result.any_live, f"no product came back live: {result.errors}"

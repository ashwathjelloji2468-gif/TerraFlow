"""Inlet buffer vs terrain (real DualSPHysics 5.4.355: JSphInOut::InitCheckProximity rejected 83
particles at z 2012..2016 next to the synthetic inlet, whose buffer started at its centre bed
2012.70 m). The buffer bottom must clear the highest terrain under the whole rotated footprint."""
from __future__ import annotations

import numpy as np
import pytest

from backend.m4_sph.generator import (InletClearanceError, build_nearfield_case, hydrograph_to_velocity,
                                      inlet_geometry, terrain_max_under_zone, zone_envelope)
from backend.m4_sph.settings import load_sph_settings
from backend.shared.grid import CanonicalGrid

DP = 2.0


def _case(synth_terrain_dir, params, sites_dir, **overrides):
    settings = load_sph_settings(dp_m=DP, t_end_s=60.0, **overrides)
    return build_nearfield_case("synth", "synth_s001", params, settings,
                                data_dir=synth_terrain_dir.parent.parent, sites_dir=sites_dir), settings


def test_terrain_max_uses_whole_rotated_footprint():
    grid = CanonicalGrid(site_id="s", grid_id="nearfield", crs_epsg=32645, origin_x=0, origin_y=100,
                         cell_size_m=10, width=10, height=10)
    frame = {"origin_x": 0.0, "origin_y": 0.0}
    dem = np.full((10, 10), 50.0, np.float32)
    dem[5, 7] = 58.0  # cell centre (75, 45): under the footprint's end, not its centre
    peak = terrain_max_under_zone((30.0, 50.0, 50.0), (40.0, 0.0, 10.0), 0.0, (50.0, 50.0), 4, DP, grid, dem, frame)
    assert peak == 58.0
    dem[0, 0] = 99.0  # far away: ignored
    assert terrain_max_under_zone((30.0, 50.0, 50.0), (40.0, 0.0, 10.0), 0.0, (50.0, 50.0), 4, DP, grid, dem, frame) == 58.0


def test_synthetic_inlet_clears_terrain_and_keeps_zsurf(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    (spec, meta), settings = _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir)
    inlet, zone = meta["inlet"], spec.inout_zones[0]
    assert inlet["local_terrain_max_z_m"] > inlet["source_bed_z_m"]  # centre bed under-estimates the footprint
    assert inlet["terrain_clearance_m"] == settings.inlet_terrain_clearance_dp * DP >= DP
    assert inlet["effective_bottom_z_m"] >= inlet["local_terrain_max_z_m"] + inlet["terrain_clearance_m"]
    assert inlet["target_zsurf_m"] == pytest.approx(inlet["source_bed_z_m"] + settings.inlet_height_m)
    assert inlet["effective_height_m"] == pytest.approx(inlet["target_zsurf_m"] - inlet["effective_bottom_z_m"])
    assert inlet["effective_height_m"] >= settings.min_inlet_height_dp * DP
    assert inlet["effective_area_m2"] == pytest.approx(settings.inlet_width_m * inlet["effective_height_m"])
    # The zone written for the solver is the effective buffer; its imposed free surface is the target.
    assert zone.point_xyz[2] == inlet["effective_bottom_z_m"] and zone.size_xyz[2] == inlet["effective_height_m"]
    assert zone.zsurf_m == inlet["target_zsurf_m"]
    # Every buffer corner (plane + layers) is >= clearance above the terrain under it.
    corners = zone_envelope(zone.point_xyz, zone.size_xyz, zone.rotate_deg, zone.rotate_center_xy, zone.layers, DP)
    assert corners[:, 2].min() - inlet["local_terrain_max_z_m"] >= inlet["terrain_clearance_m"] - 1e-9


def test_velocity_uses_effective_area(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    from backend.m2_breach.hydrograph import hydrograph

    (spec, meta), _ = _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir)
    hg = hydrograph("synth", "synth_lake", synth_hydrograph_params, sites_dir=synth_sites_dir)
    tau, v = hydrograph_to_velocity(hg.t_s, hg.q_m3s, meta["inlet"]["effective_area_m2"], 0.0, 60.0)
    written = [(tv.time_s, tv.v_ms) for tv in spec.inout_zones[0].velocity_times]
    np.testing.assert_allclose([w[1] for w in written], v)
    # Q = v * effective area (not the nominal 20 x 15 m section).
    q = np.interp(tau, hg.t_s, hg.q_m3s)
    np.testing.assert_allclose(np.asarray(v) * meta["inlet"]["effective_area_m2"], q, rtol=1e-9)
    assert meta["inlet"]["effective_area_m2"] < 20.0 * 15.0


def test_insufficient_height_fails_with_full_context(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    with pytest.raises(InletClearanceError) as e:
        _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, inlet_height_m=4.0)
    msg = str(e.value)
    for part in ("synth/synth_s001", "inlet at (", "local terrain max", "clearance", "target zsurf", "effective height"):
        assert part in msg, msg


def test_without_dp_inlet_is_unchanged(synth_terrain_dir, synth_sites_dir, synth_config_sph):
    """Backward compatibility: no dp -> no clearance applied (pre-Feature-6 geometry)."""
    import geopandas as gpd
    import json
    import rasterio

    grid = CanonicalGrid.from_json(synth_terrain_dir / "grid_nearfield.json")
    frame = json.loads((synth_terrain_dir / "nearfield_frame.json").read_text())
    with rasterio.open(synth_terrain_dir / "dem_nearfield.tif") as ds:
        dem = ds.read(1)
    line = gpd.read_file(synth_terrain_dir / "centreline.gpkg").geometry.iloc[0]
    g = inlet_geometry(tuple(synth_config_sph.domains.near_field.inflow.location.value), grid, dem, frame, line, 20.0, 15.0)
    assert g.point_xyz[2] == g.bed_z_m and g.area_m2 == 300.0

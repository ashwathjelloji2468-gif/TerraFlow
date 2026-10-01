"""Every generated inout-zone point -- plane corners and particle-layer corners -- lies inside the
particle domain GenCase actually builds (real DualSPHysics 5.4.355 aborted the synthetic case at
"Initialising InOut": outlet point (1630.3, 5.55533, 1974.68) outside particle domain Y 6..2246)."""
from __future__ import annotations

import numpy as np
import pytest

from backend.m4_sph.case_xml import InOutZone
from backend.m4_sph.generator import (InOutOutsideDomain, build_nearfield_case, generated_domain,
                                      validate_inout_within_domain, zone_envelope)
from backend.m4_sph.settings import load_sph_settings
from backend.shared.grid import CanonicalGrid

REPORTED_OUTSIDE_POINT = (1630.3, 5.55533, 1974.68)


def _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    settings = load_sph_settings(dp_m=2.0, t_end_s=60.0)
    return build_nearfield_case("synth", "synth_s001", synth_hydrograph_params, settings,
                                data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir)


def test_generated_domain_is_stl_cell_centre_extent():
    grid = CanonicalGrid(site_id="s", grid_id="nearfield", crs_epsg=32645, origin_x=0, origin_y=100,
                         cell_size_m=10, width=4, height=3)
    dem = np.full((3, 4), 100.0, np.float32)
    dem[0, 0] = 90.0
    d = generated_domain(grid, dem, dp_m=2.0, boundary_layers=3)
    assert d == {"x": (5.0, 35.0), "y": (5.0, 25.0), "z": (90.0 - 2 * 2.0, 100.0)}


def test_envelope_includes_particle_layers():
    corners = zone_envelope((0.0, 0.0, 0.0), (20.0, 0.0, 5.0), 0.0, None, layers=4, dp_m=2.0)
    assert corners.min(axis=0).tolist() == [0.0, 0.0, 0.0]
    assert corners.max(axis=0).tolist() == [20.0, 8.0, 5.0]  # layers lie 4 x 2 m behind the plane


def test_generated_inlet_and_outlet_lie_inside_the_domain(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    spec, meta = _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir)
    domain = {k: tuple(v) for k, v in meta["generated_domain_m"].items()}
    margin = meta["inout_domain_margin_m"]
    for zone in spec.inout_zones:
        c = zone_envelope(zone.point_xyz, zone.size_xyz, zone.rotate_deg, zone.rotate_center_xy, zone.layers, spec.dp_m)
        for i, axis in enumerate("xyz"):
            assert c[:, i].min() >= domain[axis][0] + margin, (axis, c[:, i].min())
            assert c[:, i].max() <= domain[axis][1] - margin, (axis, c[:, i].max())
    # The point the real solver rejected is no longer a zone corner.
    outlet = spec.inout_zones[1]
    corners = zone_envelope(outlet.point_xyz, outlet.size_xyz, outlet.rotate_deg, outlet.rotate_center_xy,
                            outlet.layers, spec.dp_m)
    assert np.min(np.linalg.norm(corners - np.asarray(REPORTED_OUTSIDE_POINT), axis=1)) > 1.0


def test_no_initial_fluid_block(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    spec, _ = _case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir)
    tags = [c.tag for c in spec.draw_commands]
    assert "fillbox" not in tags and "drawbox" not in tags  # fluid comes only from the inlet zone


def test_validation_names_zone_side_and_coordinate():
    domain = {"x": (5.0, 2035.0), "y": (6.0, 2246.0), "z": (1970.0, 2380.0)}
    bad = InOutZone(point_xyz=(1620.0, 0.0, 1974.68), size_xyz=(20.0, 0.0, 15.0), direction_xyz=(0, -1, 0),
                    layers=4, zsurf_m=1980.0, velocity_mode=2)
    with pytest.raises(InOutOutsideDomain) as e:
        validate_inout_within_domain([("outlet", bad)], 2.0, domain, margin_m=2.0)
    msg = str(e.value)
    assert "outlet zone y-min side: 0.000 < 8.000" in msg
    assert "x 5.000..2035.000" in msg
    good = InOutZone(point_xyz=(1620.0, 100.0, 1980.0), size_xyz=(20.0, 0.0, 15.0), direction_xyz=(0, -1, 0),
                     layers=4, zsurf_m=1990.0, velocity_mode=2)
    validate_inout_within_domain([("outlet", good)], 2.0, domain, margin_m=2.0)

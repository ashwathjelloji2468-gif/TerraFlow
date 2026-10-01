from __future__ import annotations

import pytest

# Re-export shared fixtures into this test subtree rather than registering sibling conftests as
# plugins (pytest 8 rejects non-root `pytest_plugins` declarations).
from tests.shared.conftest import synth_raw, write_site  # noqa: F401
from tests.m4_sph.conftest import (  # noqa: F401
    synth_config_sph, synth_hydrograph_params, synth_raw_sph, synth_sites_dir,
    synth_terrain_dir,
)


@pytest.fixture
def synth_m3_sites_dir(synth_raw_sph, write_site):
    data = synth_raw_sph
    data["domains"]["far_field"]["inflow"]["base_flow"] = {
        "value": 12.0, "unit": "m^3/s", "source": "Synthetic test fixture", "status": "placeholder",
    }
    return write_site(data, stem="synth").parent


# Feature 5 real-kernel smoke case. The shared synthetic valley is a sharp V (zero-width thalweg).
# With BedLevType 3 (bed at mesh nodes, a flow link bounded by its edge's higher node) a 30 m
# triangular mesh puts almost no nodes on that line, so every down-valley edge has one node up
# the wall: the real kernel run ponded behind such edge sills ~1.7 km below the source and never
# reached the outlet (docs/progress.md, Feature 5 acceptance). A flat valley floor of 240 m
# (8 mesh cells at 30 m) gives the mesh a continuous floor path. Synthetic -- not real terrain.
SMOKE_FLOOR_HALF_WIDTH_M = 120.0


@pytest.fixture
def synth_m3_channel_terrain_dir(tmp_path, synth_config_sph):
    """Like `synth_terrain_dir`, but over the flat-floored synthetic valley (real M1 pipeline)."""
    from backend.m1_terrain.pipeline import build_terrain
    from backend.m1_terrain.settings import TerrainSettings
    from tests.m1_terrain import synthetic_valley as sv

    cfg = synth_config_sph
    raw_dir, out_dir = tmp_path / "raw_channel", tmp_path / "data_channel" / "synth" / "terrain"
    sv.write_raw_rasters(raw_dir, cfg.domains.far_field.bbox.value, tuple(cfg.dams[0].breach_location.value),
                         tuple(cfg.points_of_interest[0].location.value), tuple(cfg.dams[0].location.value),
                         floor_half_width_m=SMOKE_FLOOR_HALF_WIDTH_M)
    sv.write_raw_provenance(raw_dir, "srtm_gl1")
    build_terrain(cfg, "srtm_gl1", raw_dir, out_dir, TerrainSettings())
    return out_dir

"""Feature 5: kernel-free diagnosis of the real-kernel smoke-case stall.

The real D-Flow FM run on the sharp-V synthetic valley stopped ~1.7 km below the source. At its
final frontier, wet face "762" (bed 2162.554625 m, water surface 2166.278832 m) did not spill
into lower dry face "782" (bed 2156.016 m). These tests rebuild the same deterministic net and
show why: the shared 762/782 edge has one node on the thalweg and one 85 m up the wall, so its
sill (higher node, BedLevType 3) is above the water surface. They also check the flat-floored
smoke case has a source-to-outlet path without such a pond."""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from backend.m3_dflowfm.generator import build_case
from backend.m3_dflowfm.hydraulic_path import source_outlet_bottleneck
from backend.m3_dflowfm.smoke import _source_xy

FRONTIER_WSE_M = 2166.278832     # face 762 water surface at t=187 min, real kernel run
FRONTIER_BED_M = 2162.554625     # face 762 bed
DRY_NEIGHBOUR_BED_M = 2156.016   # face 782 bed
MAX_PATH_PONDING_M = 1.0


def _build(terrain_dir, sites_dir, params, tmp_path):
    case, _ = build_case("synth", "synth__smoke", params, data_dir=terrain_dir.parent.parent,
                         sites_dir=sites_dir, case_dir=tmp_path / "case", stop_s=3600)
    return case


def test_v_valley_frontier_is_an_edge_sill_above_the_water_surface(synth_terrain_dir, synth_m3_sites_dir,
                                                                   synth_hydrograph_params, tmp_path):
    case = _build(synth_terrain_dir, synth_m3_sites_dir, synth_hydrograph_params, tmp_path)
    with xr.open_dataset(case / "inputs/domain_net.nc") as ds:
        nz = ds["mesh2d_node_z"].values.astype(float)
        raw = ds["mesh2d_face_nodes"].values
        start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
    faces = [{int(v) - start for v in row if np.isfinite(v)} for row in raw]
    bed = np.array([nz[list(f)].mean() for f in faces])
    f762 = int(np.argmin(abs(bed - FRONTIER_BED_M)))
    assert bed[f762] == pytest.approx(FRONTIER_BED_M, abs=1e-5)  # same net as the real run
    f782 = next(i for i, f in enumerate(faces)
                if len(f & faces[f762]) == 2 and abs(bed[i] - DRY_NEIGHBOUR_BED_M) < 1e-3)
    shared = faces[f762] & faces[f782]
    sill = max(nz[n] for n in shared)
    assert bed[f782] < FRONTIER_WSE_M < sill          # lower face, but the edge sill blocks it
    assert min(nz[n] for n in shared) < FRONTIER_WSE_M  # a wet thalweg node: not a "higher face"

    path = source_outlet_bottleneck(case / "inputs/domain_net.nc", _source_xy(case),
                                    case / "inputs/downstream_outlet.pli")
    assert path["connected"] and path["bottleneck_required_ponding_m"] > 5.0


def test_flat_floored_smoke_case_has_a_source_to_outlet_path(synth_m3_channel_terrain_dir, synth_m3_sites_dir,
                                                             synth_hydrograph_params, tmp_path):
    case = _build(synth_m3_channel_terrain_dir, synth_m3_sites_dir, synth_hydrograph_params, tmp_path)
    path = source_outlet_bottleneck(case / "inputs/domain_net.nc", _source_xy(case),
                                    case / "inputs/downstream_outlet.pli")
    assert path["connected"] and path["outlet_faces"] >= 1
    assert path["bottleneck_required_ponding_m"] <= MAX_PATH_PONDING_M, path

"""End-to-end test for `backend.m4_sph.generator.build_nearfield_case`, on the synthetic
V-shaped valley (CLAUDE.md rule 2: every module runs end-to-end on synthetic data before real
data exists). Terrain comes from a real M1 pipeline run (`synth_terrain_dir` fixture); the
hydrograph is M2's triangular fallback for the synthetic dam."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from backend.m4_sph.generator import (
    InflowUnavailable,
    OverVramBudget,
    build_nearfield_case,
    inlet_rotation_for_tangent,
    write_case,
)
from backend.m4_sph.settings import load_sph_settings
from backend.shared.site_config import load_site_config


def test_inlet_rotation_matches_dualsphysics_clockwise_axis():
    # A downstream-east channel needs a north/south inlet width. The stock solver
    # rotates clockwise, so -90 degrees turns the unrotated (0,-1) flow vector east.
    assert inlet_rotation_for_tangent(1.0, 0.0) == pytest.approx(-90.0)
    # A downstream-south channel similarly needs a west/east inlet width.
    assert inlet_rotation_for_tangent(0.0, -1.0) == pytest.approx(0.0)


def _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, **overrides):
    settings = load_sph_settings(**overrides)
    return build_nearfield_case(
        "synth", "synth_s001", synth_hydrograph_params, settings,
        data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir,
    )


def test_build_and_write_nearfield_case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, tmp_path):
    spec, case_meta = _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=2.0)

    run_dir = tmp_path / "runs" / "synth_s001__sph"
    case_dir = write_case(spec, case_meta, run_dir, synth_terrain_dir)

    xml_path = case_dir / "synth_s001__sph_Def.xml"
    assert xml_path.is_file()
    assert (case_dir / "nearfield.stl").is_file()
    assert (case_dir / "case_meta.json").is_file()

    tree = ET.parse(xml_path)
    root = tree.getroot()

    dp = float(root.find("./casedef/geometry/definition").get("dp"))
    assert dp == pytest.approx(2.0)

    time_max = next(p for p in root.findall("./execution/parameters/parameter") if p.get("key") == "TimeMax")
    assert float(time_max.get("value")) > 0

    # one inlet zone (Remove fluid) and one downstream outlet zone (Convert fluid),
    # matching DualSPHysics's own two-zone open-channel example
    # (examples/inletoutlet/02_OpenChannel) -- a lone inlet has nowhere for fluid to
    # exit and ponds at the inlet itself (docs/decisions.md "M4: SPH outlet zone").
    inout_zones = root.findall("./execution/special/inout/inoutzone")
    assert len(inout_zones) == 2
    inlet_el, outlet_el = inout_zones
    assert inlet_el.find("inputtreatment").get("value") == "2"
    assert inlet_el.find("imposevelocity").get("mode") == "1"
    assert outlet_el.find("inputtreatment").get("value") == "1"
    assert outlet_el.find("imposevelocity").get("mode") == "2"
    assert outlet_el.find("imposerhop").get("mode") == "1"
    assert outlet_el.find("imposezsurf").get("mode") == "2"
    stl_draws = root.findall("./casedef/geometry/commands/mainlist/drawfilestl")
    assert len(stl_draws) == 3  # settings.boundary_layers default
    assert all(d.get("file") == "nearfield.stl" for d in stl_draws)

    saved_meta = json.loads((case_dir / "case_meta.json").read_text())
    assert saved_meta["probes_used"] == ["synth__poi__town_a"]
    assert saved_meta["probes_skipped"] == ["synth__poi__bridge_b"]
    assert saved_meta["caveats"] == ["clear_water", "fixed_area_inlet"]


def test_inlet_mass_flux_matches_hydrograph(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    spec, case_meta = _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=2.0)
    zone = spec.inout_zones[0]
    area_m2 = case_meta["inlet"]["area_m2"]

    from backend.m2_breach.hydrograph import hydrograph as m2_hydrograph
    hydro = m2_hydrograph("synth", "synth_lake", synth_hydrograph_params, sites_dir=synth_sites_dir)

    for tv in zone.velocity_times:
        expected_q = np.interp(tv.time_s + case_meta["t_start_s"], hydro.t_s, hydro.q_m3s)
        assert tv.v_ms * area_m2 == pytest.approx(expected_q, rel=1e-6)


def test_m4_consumes_routed_m3_artifact(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, tmp_path):
    from backend.m3_dflowfm.routed_discharge import write_routed_discharge

    _, sidecar = write_routed_discharge(
        tmp_path / "runs" / "m3", site_id="synth", scenario_id="synth_s001",
        source_run_id="synth_s001__delft3d", t_s=[0, 600, 1200, 1800],
        q_m3s=[20, 80, 40, 10], routing_method="controlled_map_section_integration",
        section={"type": "LineString", "coordinates": [[0, 0], [10, 10]], "crs": "EPSG:32645"},
        provenance={"source_map": "controlled-m3-output-fixture"},
    )
    spec, meta = build_nearfield_case(
        "synth", "synth_s001", synth_hydrograph_params, load_sph_settings(dp_m=2.0),
        data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir,
        routed_discharge_path=sidecar,
    )
    zone = spec.inout_zones[0]
    area = meta["inlet"]["area_m2"]
    expected = np.interp(
        np.asarray([v.time_s for v in zone.velocity_times]) + meta["t_start_s"],
        [0, 600, 1200, 1800], [20, 80, 40, 10],
    )
    np.testing.assert_allclose(np.asarray([v.v_ms for v in zone.velocity_times]) * area, expected)
    assert meta["provenance"]["hydrograph_method"] == "m3_routed_discharge"
    assert meta["provenance"]["routed_discharge"]["source_m3_run_id"] == "synth_s001__delft3d"


def test_outlet_is_downstream_of_inlet_and_spans_domain_height(
    synth_terrain_dir, synth_hydrograph_params, synth_sites_dir,
):
    spec, case_meta = _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=2.0)
    inlet_zone, outlet_zone = spec.inout_zones
    # The outlet sits further along the flow (SPH-frame y here, per the synthetic V-valley
    # centreline) than the inlet, not on top of it.
    assert outlet_zone.point_xyz[1] != pytest.approx(inlet_zone.point_xyz[1])
    # It uses the same depth ceiling as the inlet (inlet_height_m), not the domain's full
    # terrain relief (which can include valley walls far above any plausible flood surface,
    # and would blow the particle/VRAM budget for no modelling benefit).
    settings = load_sph_settings(dp_m=2.0)
    assert outlet_zone.size_xyz[2] == pytest.approx(settings.inlet_height_m)
    # Convert-fluid outlet, not remove-fluid: a real fluid particle found in the outlet's
    # footprint should be turned into an inout ghost particle, never hard-deleted.
    assert outlet_zone.inputtreatment == 1
    assert inlet_zone.inputtreatment == 2


def test_gauges_present_for_every_probe_kept(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    spec, case_meta = _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=2.0)
    assert {g.name for g in spec.swl_gauges} == {"swl_synth__poi__town_a"}
    assert {g.name for g in spec.vel_gauges} == {"vel_synth__poi__town_a"}


def test_placeholders_and_provenance_recorded(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    _, case_meta = _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=2.0)
    cfg = load_site_config("synth", sites_dir=synth_sites_dir)
    assert case_meta["has_placeholders"] == cfg.has_placeholders
    assert case_meta["placeholder_fields"] == cfg.placeholder_fields
    assert case_meta["provenance"]["hydrograph_method"] == "triangular"


def test_far_field_inflow_raises(synth_terrain_dir, synth_hydrograph_params, tmp_path):
    from tests.shared.conftest import fully_sourced
    import yaml

    with open("tests/fixtures/shared/synth.yaml", encoding="utf-8") as f:
        raw = fully_sourced(yaml.safe_load(f))  # inflow.from stays "far_field"
    sites_dir = tmp_path / "far_field_sites"
    sites_dir.mkdir()
    (sites_dir / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(InflowUnavailable, match="far_field"):
        build_nearfield_case("synth", "synth_s001", synth_hydrograph_params,
                              data_dir=synth_terrain_dir.parent.parent, sites_dir=sites_dir)


def test_tiny_dp_over_vram_budget_raises(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    with pytest.raises(OverVramBudget):
        _build(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, dp_m=0.001)


def test_auto_dp_stays_within_budget(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    settings = load_sph_settings()
    assert settings.dp_m == "auto"
    spec, case_meta = build_nearfield_case(
        "synth", "synth_s001", synth_hydrograph_params, settings,
        data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir,
    )
    assert spec.dp_m > 0
    assert case_meta["vram_vram_mib"] <= case_meta["vram_budget_mib"]

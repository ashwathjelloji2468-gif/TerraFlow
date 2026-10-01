"""Feature 6 unit tests: solver-log provenance, particle retention, run_meta facts, data-dir default,
Feature 5 → M4 section discharge, campaign wiring and the generic SPH ↔ Delft3D pair comparison.

None of these runs or imitates a solver: logs are the real retained DualSPHysics logs in
`backend/m4_pilot/`, and NetCDF maps are small hand-built UGRID files with known answers. The real
solver test is `test_real_solver.py`."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import LineString

from backend.m4_sph import compare, solver_log
from backend.m4_sph import postprocess as pp
from backend.shared.grid import FLOAT_NODATA, CanonicalGrid

REPO = Path(__file__).resolve().parents[2]
PILOT_LOG = REPO / "backend/m4_pilot/dambreak3d_dp0p0125/dualsphysics_output.log"


# --- solver log / provenance --------------------------------------------------------------------
def test_parse_real_solver_log():
    facts = solver_log.parse_log(PILOT_LOG.read_text(errors="replace"))
    assert facts["solver_version"] == "5.4.355"  # not the references section's "v5.0"
    assert facts["gencase_version"] == "5.4.354.01"
    assert facts["gpu_name"] == "NVIDIA GeForce RTX 4060 Laptop GPU"
    assert (facts["cuda_driver_version"], facts["cuda_runtime_version"]) == ("13.1", "12.8")
    assert (facts["total_particles"], facts["bound_particles"], facts["fluid_particles"]) == (60887, 20183, 40704)
    assert facts["excluded_particles"] == 0 and facts["final_part_particles"] == 60887
    assert facts["finished_code"] == 0 and facts["exclusion_warning"] is False


def test_run_meta_solver_version_regression():
    # The old regex read "5.0" from the log's citation list.
    assert pp._parse_solver_log(PILOT_LOG.read_text(errors="replace"))["solver_version"] == "5.4.355"


def test_missing_log_facts_stay_none():
    facts = solver_log.parse_log(None)
    assert all(v is None for k, v in facts.items() if k != "exclusion_warning")
    assert solver_log.particle_retention(facts, 0.05)["status"] == "UNAVAILABLE"


def _log(excluded: int, final: int, peak: int, warning: bool = False) -> str:
    return (f"Total particles: {peak:,} (bound=100 (fx=100 mv=0 ft=0) fluid={peak - 100:,})\n"
            f"00001   0.010008           78       79       {final:,}        1,408      12.41  x\n"
            + (f"{solver_log.EXCLUSION_WARNING} in Part_0003\n" if warning else "")
            + f"Excluded particles...............: {excluded:,}\nMaximum number of particles......: {peak:,}\n")


@pytest.mark.parametrize("excluded,final,peak,warning,status", [
    (0, 1000, 1000, False, "SUCCEEDED"),
    (40, 960, 1000, False, "SUCCEEDED"),     # 4 % <= 5 %
    (540_237, 5_000, 545_000, False, "FAILED"),  # the Teesta a02 magnitude
    (0, 1000, 1000, True, "FAILED"),          # solver's own >100 % warning
    (0, 100, 1000, False, "FAILED"),          # only boundary particles left
])
def test_particle_retention(excluded, final, peak, warning, status):
    r = solver_log.particle_retention(solver_log.parse_log(_log(excluded, final, peak, warning)), 0.05)
    assert r["status"] == status, r


def test_run_meta_uses_measured_vram_and_wall_time():
    grid = CanonicalGrid(site_id="synth", grid_id="nearfield", crs_epsg=32645, origin_x=0, origin_y=10,
                         cell_size_m=1, width=2, height=2)
    case_meta = {"scenario_id": "s1", "dp_m": 0.5, "caveats": [], "has_placeholders": True, "placeholder_fields": []}
    meta = pp.build_run_meta(case_meta, grid, 10.0, False, PILOT_LOG.read_text(errors="replace"),
                             execution={"peak_vram_mb": 845.0, "wall_time_s": 10.9},
                             max_excluded_particle_fraction=0.05)
    assert meta["peak_vram_mb"] == 845.0 and meta["wall_time_s"] == 10.9
    assert not any("peak_vram_mb not measured" in w for w in meta["warnings"])
    assert meta["particle_retention"]["status"] == "SUCCEEDED"
    without = pp.build_run_meta(case_meta, grid, 10.0, False, None)
    assert without["peak_vram_mb"] is None and any("peak_vram_mb not measured" in w for w in without["warnings"])


def test_launcher_records_provenance_on_failure(tmp_path):
    """Controlled shell stand-ins (not solver validation): the failure record still carries the
    executables and every fact its solver log printed, and the log is kept in raw/."""
    from backend.m4_sph.launcher import run_case_sync

    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "GenCase_linux64").write_text('#!/bin/sh\nprintf "<c/>" > "$2.xml"\nprintf x > "$2.bi4"\n'
                                          'echo "GenCase v5.4.354.01"\necho "Total particles: 10 (bound=4 (fx=4) fluid=6)"\n')
    (bins / "DualSPHysics5.4_linux64").write_text('#!/bin/sh\necho "DualSPHysics5 v5.4.355 (x)"\necho "*** Exception"\nexit 3\n')
    for f in bins.iterdir():
        f.chmod(0o755)
    case = tmp_path / "run" / "case"
    case.mkdir(parents=True)
    (case / "c_Def.xml").write_text("<case/>")
    result = run_case_sync(case, tmp_path / "run" / "attempts" / "a00", bins)
    assert result["success"] is False and result["solver_returncode"] == 3
    prov = result["provenance"]
    assert prov["solver_executable"] == str(bins.resolve() / "DualSPHysics5.4_linux64")
    assert prov["gencase_version"] == "5.4.354.01" and prov["gencase_particles"]["total_particles"] == 10
    assert prov["solver_log_facts"]["solver_version"] == "5.4.355"
    assert (tmp_path / "run" / "raw" / "log.txt").is_file()


def test_generator_default_data_dir_is_sih26_data_dir(monkeypatch, tmp_path):
    from backend.m4_sph import generator

    assert not hasattr(generator, "DATA_DIR")
    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "elsewhere"))
    with pytest.raises(FileNotFoundError) as e:
        generator.build_nearfield_case("synth", "s", {}, sites_dir=REPO / "tests/fixtures/shared")
    assert str(tmp_path / "elsewhere") in str(e.value)


# --- synthetic D-Flow FM run --------------------------------------------------------------------
def _dflow_run(root: Path, run_id: str, spinup_s: float = 100.0, depth=(0.0, 0.5, 1.0), speed=(0.0, 2.0, 2.0)):
    """One 100 m square face (x 0..100, y 0..100) with known depth/speed at t = 0, 100, 200 s."""
    run = root / run_id
    out = run / "case" / "output"
    out.mkdir(parents=True)
    (run / "case" / "case_meta.json").write_text(json.dumps({"spinup_s": spinup_s}))
    (run / "run_meta.json").write_text(json.dumps({"status": "postprocessed", "thresholds": {"extent_m": 0.3, "arrival_m": 0.1}}))
    times = np.datetime64("2001-01-01T00:00:00", "ns") + np.array([0, 100, 200]) * np.timedelta64(1, "s")
    xr.Dataset({"mesh2d_node_x": ("n", [0.0, 100.0, 100.0, 0.0]), "mesh2d_node_y": ("n", [0.0, 0.0, 100.0, 100.0]),
                "mesh2d_face_nodes": (("f", "k"), [[0, 1, 2, 3]], {"start_index": 0}),
                "mesh2d_waterdepth": (("time", "f"), np.array(depth)[:, None]),
                "mesh2d_ucmag": (("time", "f"), np.array(speed)[:, None])},
               coords={"time": times}).to_netcdf(out / "model_map.nc")
    xr.Dataset({"waterdepth": (("time", "s"), np.array(depth)[:, None]), "station_id": ("s", np.array([b"poi_a"]))},
               coords={"time": times}).to_netcdf(out / "model_his.nc")
    return run


def test_section_discharge_integrates_and_removes_spinup(tmp_path):
    from backend.m3_dflowfm.section_discharge import section_discharge

    run = _dflow_run(tmp_path, "s__delft3d")
    r = section_discharge(run, LineString([(50, -10), (50, 110)]), crs_epsg=32645)
    np.testing.assert_allclose(r["t_s"], [0.0, 100.0])  # t = 100, 200 s minus 100 s spin-up
    np.testing.assert_allclose(r["q_m3s"], [0.5 * 2.0 * 100.0, 1.0 * 2.0 * 100.0])  # depth × speed × 100 m cut
    assert r["faces_crossed"] == 1


def test_write_section_discharge_keeps_speed_caveat(tmp_path):
    from backend.m3_dflowfm.routed_discharge import read_routed_discharge
    from backend.m3_dflowfm.section_discharge import SPEED_CAVEAT, write_section_discharge

    run = _dflow_run(tmp_path, "s__delft3d")
    _, sidecar = write_section_discharge(run, tmp_path / "routed", LineString([(50, -10), (50, 110)]),
                                         site_id="synth", scenario_id="s", source_run_id="s__delft3d",
                                         crs_epsg=32645, section_id="sec")
    t, q, record = read_routed_discharge(sidecar, site_id="synth", scenario_id="s")
    assert record["provenance"]["normal_velocity_assumption"] == SPEED_CAVEAT
    assert record["provenance"]["velocity_vector_available"] is False
    assert "teesta" not in json.dumps(record)


def test_section_off_mesh_is_an_error(tmp_path):
    from backend.m3_dflowfm.section_discharge import section_discharge

    with pytest.raises(ValueError, match="does not cross"):
        section_discharge(_dflow_run(tmp_path, "s__delft3d"), LineString([(500, 0), (500, 10)]), crs_epsg=32645)


def test_sph_campaign_refuses_far_field_inflow_without_delft3d_run(synth_terrain_dir, monkeypatch, write_site, synth_raw_sph):
    from backend import campaign
    from backend.m0_api import registry

    raw = json.loads(json.dumps(synth_raw_sph))
    raw["domains"]["near_field"]["inflow"]["from"] = "far_field"
    raw.setdefault("simulation", {}).setdefault("sph", {})["scenarios"] = ["synth__s001"]
    sites = write_site(raw, stem="synth").parent
    data = synth_terrain_dir.parent.parent
    (data / "synth" / "design").mkdir(parents=True)
    (data / "synth" / "design" / "scenario_design.json").write_text(json.dumps(
        {"scenarios": [{"scenario_id": "synth__s001", "params": {"water_volume_m3": 1.0}}], "extra": []}))
    monkeypatch.setenv("SIH26_DATA_DIR", str(data))
    registry.init_db()
    _, results = campaign.run_sph_campaign("synth", registry.connect(), data, sites)
    assert results[0].status == "refused"
    assert "no routed Delft3D inflow" in results[0].reason and "synth__s001__delft3d" in results[0].reason


# --- generic comparison -------------------------------------------------------------------------
def test_pair_metrics_known_answer():
    nd = FLOAT_NODATA
    mask = np.array([[True, True, True, False]])
    d_depth = np.array([[1.0, 0.0, 0.5, nd]], dtype=np.float32)
    s_depth = np.array([[1.0, 0.4, 0.0, 0.9]], dtype=np.float32)
    vel = np.array([[1.0, 1.0, 1.0, nd]], dtype=np.float32)
    m, valid = compare.pair_metrics(mask, d_depth, vel, s_depth, vel * 2, 10.0, 0.3, nd)
    assert valid.sum() == 3
    assert m["iou"] == pytest.approx(1 / 3) and m["f1"] == pytest.approx(0.5)
    assert m["depth_rmse_wet_m"] == pytest.approx(np.sqrt((0 + 0.16 + 0.25) / 3), rel=1e-5)
    assert m["velocity_mae_ms"] == pytest.approx(1.0)
    assert m["flooded_area_delft3d_m2"] == 200.0 and m["flooded_area_sph_m2"] == 200.0


def test_delft3d_window_uses_seconds_since_t0(tmp_path):
    grid = CanonicalGrid(site_id="synth", grid_id="nearfield", crs_epsg=32645, origin_x=0, origin_y=100,
                         cell_size_m=50, width=2, height=2)
    run = _dflow_run(tmp_path, "s__delft3d")
    _, depth, _, used = compare._delft3d_window_on_grid(run, grid, 0.0, 100.0)  # records at 100, 200 s
    np.testing.assert_allclose(used, [0.0, 100.0])
    assert float(depth.max()) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="fewer than two records"):
        compare._delft3d_window_on_grid(run, grid, 150.0, 400.0)


def _sph_run(root: Path, scenario: str, *, success: bool, caveats=(), retention="SUCCEEDED"):
    run = root / f"{scenario}__sph"
    (run / "attempts" / "a00").mkdir(parents=True)
    (run / "attempts" / "a00" / "execution.json").write_text(json.dumps({"success": success, "error": None if success else "x"}))
    if success:
        (run / "run_meta.json").write_text(json.dumps({"status": "postprocessed", "caveats": list(caveats),
                                                       "particle_retention": {"status": retention}}))
    return run


def test_statuses_are_separate_and_validation_never_automatic(tmp_path):
    runs = tmp_path / "synth" / "runs"
    _dflow_run(runs, "s__delft3d")
    _sph_run(runs, "s", success=True)
    st = compare.pair_statuses(tmp_path, "synth", "s")
    assert (st["solver_status"], st["postprocess_status"]) == ("SUCCEEDED", "SUCCEEDED")
    assert st["comparison_reason"] is None and st["validation_status"] == "BLOCKED"


@pytest.mark.parametrize("kwargs", [{"caveats": ["sph_particle_exclusion_warning"]}, {"retention": "FAILED"}])
def test_particle_retention_gate_blocks_comparison(tmp_path, kwargs):
    runs = tmp_path / "synth" / "runs"
    _dflow_run(runs, "s__delft3d")
    _sph_run(runs, "s", success=True, **kwargs)
    sidecar = compare.build_pair_comparison(tmp_path, "synth", "s")
    result = json.loads(sidecar.read_text())
    sv = result["sph_vs_delft3d"]
    assert sv["available"] is False and sv["metrics"] == {}
    assert sv["status"]["comparison_status"] == "BLOCKED" and "retention gate" in sv["status"]["comparison_reason"]
    from backend.m0_api import schemas
    schemas.validate("compare.schema.json", result)


def test_failed_sph_solver_is_reported_failed(tmp_path):
    runs = tmp_path / "synth" / "runs"
    _dflow_run(runs, "s__delft3d")
    _sph_run(runs, "s", success=False)
    st = compare.pair_statuses(tmp_path, "synth", "s")
    assert st["solver_status"] == "FAILED" and st["postprocess_status"] == "BLOCKED"
    assert compare.status_only_response(tmp_path, "synth", "s")["sph_vs_delft3d"]["run_ids"] == ["s__delft3d", "s__sph"]
    assert compare.status_only_response(tmp_path, "synth", "nothing") is None


def test_pair_comparison_computes_metrics_from_artifacts(tmp_path):
    """Both runs postprocessed and retained: metrics come from the two runs' own files."""
    from backend.m0_api import schemas
    from backend.shared.grid import write_grid_raster

    grid = CanonicalGrid(site_id="synth", grid_id="nearfield", crs_epsg=32645, origin_x=0, origin_y=100,
                         cell_size_m=50, width=2, height=2)
    (tmp_path / "synth" / "terrain").mkdir(parents=True)
    grid.to_json(tmp_path / "synth" / "terrain" / "grid_nearfield.json")
    runs = tmp_path / "synth" / "runs"
    _dflow_run(runs, "s__delft3d")
    sph = _sph_run(runs, "s", success=True)
    (sph / "case").mkdir()
    (sph / "case" / "case_meta.json").write_text(json.dumps({"t_start_s": 0.0, "t_end_s": 100.0}))
    (sph / "summary_nearfield").mkdir()
    write_grid_raster(sph / "summary_nearfield" / "max_depth.tif", np.full((2, 2), 0.8, np.float32), grid)
    write_grid_raster(sph / "summary_nearfield" / "max_velocity.tif", np.full((2, 2), 1.5, np.float32), grid)

    result = json.loads(compare.build_pair_comparison(tmp_path, "synth", "s").read_text())
    schemas.validate("compare.schema.json", result)
    sv = result["sph_vs_delft3d"]
    assert sv["available"] is True and sv["status"]["comparison_status"] == "SUCCEEDED"
    assert sv["status"]["validation_status"] == "BLOCKED"
    m = sv["metrics"]
    assert m["common_valid_cell_count"] == 4 and m["iou"] == 1.0
    assert m["depth_rmse_wet_m"] == pytest.approx(0.2, rel=1e-5)   # 0.8 vs window max 1.0
    assert m["velocity_mae_ms"] == pytest.approx(0.5, rel=1e-5)    # 1.5 vs 2.0
    assert sv["run_ids"] == ["s__delft3d", "s__sph"] and sv["time_window_s"] == 100.0
    assert sv["probes"] == []  # no SPH timeseries.csv -> no arrival metric invented


def test_inlet_burial_measures_plane_below_terrain(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    """Mechanism-1 evidence check on the synthetic case: the inlet plane is anchored at its centre
    bed, so on a sloped cross-section part of it lies under the terrain."""
    import geopandas as gpd
    import rasterio

    from backend.m4_sph.exclusion_diagnosis import inlet_burial
    from backend.m4_sph.generator import build_nearfield_case
    from backend.m4_sph.settings import load_sph_settings

    settings = load_sph_settings(dp_m=2.0, t_end_s=60.0)
    _, meta = build_nearfield_case("synth", "synth_s001", synth_hydrograph_params, settings,
                                   data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir)
    grid = CanonicalGrid.from_json(synth_terrain_dir / "grid_nearfield.json")
    with rasterio.open(synth_terrain_dir / "dem_nearfield.tif") as ds:
        dem = ds.read(1)
    line = gpd.read_file(synth_terrain_dir / "centreline.gpkg").geometry.iloc[0]
    b = inlet_burial(meta, grid, dem, line, settings.inlet_width_m)
    assert b["valid_samples"] == b["samples"]
    assert 0.0 < b["fraction_of_width_bed_above_inlet_bottom"] < 1.0
    assert 0.0 < b["buried_inlet_area_fraction"] < 1.0

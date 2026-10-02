"""Feature 6: real DualSPHysics smoke test on the synthetic TerraFlow near-field case.

Uses the real GenCase, DualSPHysics, MeasureTool and IsoSurface binaries from `DSPH_BIN_DIR` and a
case built by the production generator. No fake binaries, no monkeypatching. Skips (reason shown
with `pytest -rs`) without the binaries or an NVIDIA GPU. On the team Linux/WSL machine:

    DSPH_BIN_DIR=<DualSPHysics_v5.4>/bin/linux pytest -rs -s tests/m4_sph/test_real_solver.py

Run settings are test choices, not physical values: dp 2 m keeps the case small (and leaves room for the
inlet's 1-dp terrain clearance and 3-dp minimum height), 60 s of simulated inflow, PART output every 5 s.
"""
from __future__ import annotations

import json
import shutil

import pytest

from backend.m4_sph import launcher, smoke
from backend.m4_sph.generator import build_nearfield_case, write_case
from backend.m4_sph.settings import load_sph_settings

SMOKE_DP_M = 2.0
SMOKE_T_END_S = 60.0


def _skip_reason() -> str | None:
    try:
        smoke.resolve_binaries()
    except smoke.BinariesUnavailable as e:
        return f"real DualSPHysics binaries unavailable: {e}"
    if shutil.which("nvidia-smi") is None:
        return "nvidia-smi not found: the DualSPHysics GPU solver needs an NVIDIA GPU"
    return None


_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


def test_real_dualsphysics_runs_synthetic_nearfield_case(synth_terrain_dir, synth_hydrograph_params,
                                                         synth_sites_dir, tmp_path, record_property):
    assert smoke.launcher.run_case_sync is launcher.run_case_sync  # the production launcher
    settings = load_sph_settings(dp_m=SMOKE_DP_M, t_end_s=SMOKE_T_END_S)
    spec, case_meta = build_nearfield_case("synth", "synth_s001", synth_hydrograph_params, settings,
                                           data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir)
    run_dir = tmp_path / "run"
    case_dir = write_case(spec, case_meta, run_dir, synth_terrain_dir)

    report = smoke.run_smoke_case(case_dir, run_dir, synth_terrain_dir, settings)
    print(json.dumps(report, indent=2, default=str))
    execution = report["execution"]
    prov = execution["provenance"]
    for key in ("wall_time_s", "peak_vram_mb", "gencase_returncode", "solver_returncode"):
        record_property(key, execution.get(key))
    record_property("solver_version", prov["solver_log_facts"]["solver_version"])
    record_property("gpu_name", prov["solver_log_facts"]["gpu_name"])
    record_property("particle_retention", json.dumps(report["particle_retention"]))

    # GenCase
    assert execution["gencase_returncode"] == 0
    assert (case_dir / "synth_s001__sph.xml").stat().st_size > 0
    assert prov["gencase_version"] and prov["gencase_particles"]["total_particles"]
    # Solver + raw artifacts
    assert report["solver_status"] == "SUCCEEDED", execution["error"]
    assert execution["solver_returncode"] == 0
    assert (run_dir / "raw" / "Run.out").stat().st_size > 0
    assert list((run_dir / "raw" / "data").glob("Part_*.bi4"))
    assert prov["solver_log_facts"]["solver_version"] and prov["solver_executable"].endswith("DualSPHysics5.4_linux64")
    # Post-processing
    assert report["postprocess_status"] == "SUCCEEDED", report.get("postprocess_error")
    assert (run_dir / "summary_nearfield" / "max_depth.tif").is_file()
    assert report["run_meta"]["solver_version"] == prov["solver_log_facts"]["solver_version"]
    # Particle retention: the solver must keep its water.
    assert report["particle_retention_status"] == "SUCCEEDED", report["particle_retention"]

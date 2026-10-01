"""Feature 5 phase 1: real D-Flow FM kernel smoke test.

Runs the *real* kernel named by `SIH26_DFLOWFM_KERNEL` on a tiny synthetic case built by the
production generator (`generator.build_case`) and launched by the production launcher
(`launcher.launch_case`, through `m3_dflowfm.smoke.run_smoke_case`). No fake solver, no
`SIH26_FAKE_*`, no monkeypatching, no MVP forcing, no frozen pilot output.

Skips (with the reason shown by `pytest -rs`) when the kernel is not configured -- e.g. on Apple
Silicon, where the Linux x86_64 build cannot run. On the team's Linux/WSL machine:

    SIH26_DFLOWFM_KERNEL=~/delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh \
        pytest -rs -s tests/m3_dflowfm/test_real_solver.py

The case's base flow (12 m^3/s) is the synthetic fixture's own placeholder (`synth_m3_sites_dir`),
not a site value; the case is a numerical smoke test, not a physical result.
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from backend.m3_dflowfm import launcher, smoke
from backend.m3_dflowfm.generator import SPINUP_S, build_case


def _skip_reason() -> str | None:
    value = os.environ.get("SIH26_DFLOWFM_KERNEL")
    if not value:
        return (f"SIH26_DFLOWFM_KERNEL is not set ({platform.system()} {platform.machine()}): the real "
                "D-Flow FM 1.2.184 kernel (Linux x86_64 / WSL) is required for this test")
    try:
        smoke.resolve_kernel(value)
    except smoke.KernelUnavailable as e:
        return f"SIH26_DFLOWFM_KERNEL is unusable: {e}"
    if not Path("/usr/bin/time").is_file():
        return "/usr/bin/time is missing: the launcher cannot record output/resource_usage.txt"
    return None


_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


def test_real_kernel_runs_tiny_synthetic_case(synth_terrain_dir, synth_m3_sites_dir, synth_hydrograph_params,
                                              tmp_path, monkeypatch, record_property):
    for key in [k for k in os.environ if k.startswith("SIH26_FAKE_")]:
        monkeypatch.delenv(key)
    # The production launcher, not a substitute.
    assert launcher.launch_case.__module__ == "backend.m3_dflowfm.launcher"
    assert smoke.launcher.launch_case is launcher.launch_case

    kernel = smoke.resolve_kernel()
    case, meta = build_case("synth", "synth__smoke", synth_hydrograph_params,
                            data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_m3_sites_dir,
                            case_dir=tmp_path / "case", stop_s=3600)
    assert meta["mesh"]["face_count"] < 10_000  # genuinely tiny

    report = smoke.run_smoke_case(case, tmp_path / "run", terrain_dir=synth_terrain_dir, kernel=kernel,
                                  spinup_s=SPINUP_S, run_id="synth__smoke__dflowfm",
                                  scenario_id="synth__smoke", timeout_s=1800)
    print(json.dumps(report, indent=2, default=str))
    record_property("wall_time_s", report["wall_time_s"])
    record_property("harness_wall_time_s", report["harness_wall_time_s"])
    record_property("kernel", str(kernel))
    record_property("face_count", meta["mesh"]["face_count"])

    output = case / "output"
    # Solver verdict (M3 rule 1).
    assert report["timed_out"] is False
    dia = (output / "model.dia").read_text(errors="replace")
    assert not [line for line in dia.splitlines() if line.startswith("** ERROR")]
    assert (output / "model_map.nc").is_file()
    assert (output / "model_his.nc").is_file()
    assert (output / "resource_usage.txt").is_file()
    assert report["solver_success"] is True

    # The output came from the configured kernel, during this launch.
    prov = report["kernel_provenance"]
    assert prov["verified"] is True, prov["checks"]
    assert str(kernel) in prov["resource_usage"]["command"]
    assert report["wall_time_s"] is not None and report["wall_time_s"] > 0

    # Water actually reached the downstream half of the domain.
    assert report["downstream"]["wet_downstream_faces"] >= 1, report["downstream"]

    # Separate verdicts, all reported.
    assert report["postprocess_success"] is True, report.get("postprocess_error")
    assert (tmp_path / "run" / "summary" / "max_depth.tif").is_file()
    assert isinstance(report["wet_at_pois"]["all_wet"], bool)  # reported, not required, for a smoke case
    assert (tmp_path / "run" / "smoke_report.json").is_file()

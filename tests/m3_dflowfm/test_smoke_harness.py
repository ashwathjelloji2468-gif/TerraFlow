"""Feature 5 phase 1: the smoke harness's evidence parsing. These tests never run or imitate a
solver; they check that the harness reads real-run evidence correctly and refuses to proceed
without a real kernel. The real run itself is tests/m3_dflowfm/test_real_solver.py."""
from __future__ import annotations

import time

import numpy as np
import pytest
import xarray as xr

from backend.m3_dflowfm import smoke

USAGE = '''\tCommand being timed: "/opt/k/run_dflowfm.sh model.mdu"
\tMaximum resident set size (kbytes): 2048
\tElapsed (wall clock) time (h:mm:ss or m:ss): 1:02.50
\tExit status: 0
'''


def test_resolve_kernel_requires_env_or_argument(monkeypatch, tmp_path):
    monkeypatch.delenv("SIH26_DFLOWFM_KERNEL", raising=False)
    with pytest.raises(smoke.KernelUnavailable, match="not set"):
        smoke.resolve_kernel()
    with pytest.raises(smoke.KernelUnavailable, match="not found"):
        smoke.resolve_kernel(tmp_path / "missing.sh")
    k = tmp_path / "k.sh"
    k.write_text("")
    k.chmod(0o644)
    with pytest.raises(smoke.KernelUnavailable, match="not executable"):
        smoke.resolve_kernel(k)


def test_run_smoke_case_refuses_without_kernel_and_launches_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("SIH26_DFLOWFM_KERNEL", raising=False)
    monkeypatch.setattr(smoke.launcher, "launch_case", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(smoke.KernelUnavailable):
        smoke.run_smoke_case(tmp_path / "case", tmp_path / "run", terrain_dir=tmp_path)
    assert not (tmp_path / "run").exists()


def test_parse_resource_usage(tmp_path):
    p = tmp_path / "resource_usage.txt"
    p.write_text(USAGE)
    usage = smoke.parse_resource_usage(p)
    assert usage == {"present": True, "command": "/opt/k/run_dflowfm.sh model.mdu", "exit_status": 0,
                     "wall_time_s": 62.5, "peak_ram_mb": 2.0}
    assert smoke.parse_resource_usage(tmp_path / "absent.txt") == {"present": False}


def _output(tmp_path, usage: str, program: str = "D-Flow FM") -> None:
    out = tmp_path / "output"
    out.mkdir()
    (out / "resource_usage.txt").write_text(usage)
    (out / "model.dia").write_text(f"Program = {program}   # Program\nVersion = 1.2.184.Unknown  # Version\n")


def test_kernel_provenance_verified_only_for_the_configured_kernel(tmp_path):
    started = time.time()
    _output(tmp_path, USAGE)
    good = smoke.kernel_provenance(tmp_path, "model", smoke.Path("/opt/k/run_dflowfm.sh"), started)
    assert good["verified"] is True and good["dia"]["version"] == "1.2.184.Unknown"
    other = smoke.kernel_provenance(tmp_path, "model", smoke.Path("/other/run_dflowfm.sh"), started)
    assert other["verified"] is False
    assert other["checks"]["command_names_configured_kernel"] is False


def test_kernel_provenance_rejects_stale_dia(tmp_path):
    _output(tmp_path, USAGE)
    later = time.time() + 3600
    prov = smoke.kernel_provenance(tmp_path, "model", smoke.Path("/opt/k/run_dflowfm.sh"), later)
    assert prov["checks"]["dia_written_after_launch"] is False and prov["verified"] is False


def test_kernel_provenance_rejects_other_program(tmp_path):
    _output(tmp_path, USAGE, program="Something Else")
    prov = smoke.kernel_provenance(tmp_path, "model", smoke.Path("/opt/k/run_dflowfm.sh"), time.time() - 5)
    assert prov["checks"]["dia_program_is_dflowfm"] is False


def test_downstream_wet_faces_and_wet_at_pois_read_netcdf(tmp_path):
    # Three faces on a line: source at x=0, outlet at x=100. Only the x=90 face is wet.
    xr.Dataset({"mesh2d_face_x": ("f", [5.0, 40.0, 90.0]), "mesh2d_face_y": ("f", [0.0, 0.0, 0.0]),
                "mesh2d_waterdepth": (("time", "f"), [[0, 0, 0], [0, 0, 0.25]])},
               coords={"time": [0.0, 60.0]}).to_netcdf(tmp_path / "m_map.nc")
    d = smoke.downstream_wet_faces(tmp_path / "m_map.nc", (0.0, 0.0), (100.0, 0.0))
    assert d["downstream_faces"] == 1 and d["wet_downstream_faces"] == 1
    assert d["max_downstream_depth_m"] == pytest.approx(0.25)

    xr.Dataset({"waterdepth": (("time", "s"), [[0.5, 0.0], [0.0, 0.05], [0.0, 0.2]]),
                "station_id": ("s", np.array([b"a", b"b"]))},
               coords={"time": [0.0, 60.0, 120.0]}).to_netcdf(tmp_path / "m_his.nc")
    w = smoke.wet_at_pois(tmp_path / "m_his.nc", spinup_s=60.0, threshold_m=0.1)
    assert w["per_poi"]["a"] == {"max_depth_m": 0.0, "wet": False}  # wet only during spin-up
    assert w["per_poi"]["b"]["wet"] is True
    assert w["all_wet"] is False

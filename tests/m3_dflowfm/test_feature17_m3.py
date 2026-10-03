"""Feature 17: M3 execution truthfulness (synthetic fixtures only; no solver runs here).

- simulation_completed: a run with no ** ERROR line and both result files, but whose output stops
  before TStop (killed / crashed silently), is NOT accepted -- solver success != completion.
- solver version from the .dia, wall-time parsing, spin-up from case_meta, failed-attempt archival,
  pilot registration refusing a truncated run.
"""
from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from backend.m3_dflowfm import acceptance, launcher

from .acceptance_fixtures import make_run


def _evaluate(r):
    return acceptance.evaluate_run(r["run_dir"], run_id=r["run_id"], scenario_id=r["scenario_id"],
                                   site_dir=r["site_dir"], case_dir=r["case_dir"], model_stem="model",
                                   terrain_dir=r["terrain_dir"])


def test_simulation_completed_is_a_required_check(tmp_path):
    assert "simulation_completed" in acceptance.REQUIRED_CHECKS
    out = _evaluate(make_run(tmp_path))
    assert out["status"] == acceptance.ACCEPTED
    assert out["checks"]["simulation_completed"]["status"] == acceptance.PASS


def test_truncated_run_passes_solver_rule_but_fails_acceptance(tmp_path):
    out = _evaluate(make_run(tmp_path, truncated=True))
    assert out["checks"]["solver_success"]["status"] == acceptance.PASS  # M3 rule 1 alone is fooled
    assert out["checks"]["simulation_completed"]["status"] == acceptance.FAIL
    assert out["status"] == acceptance.FAILED_ACCEPTANCE and "simulation_completed" in out["failed_checks"]


def test_missing_stop_time_is_unavailable_never_pass(tmp_path):
    out = _evaluate(make_run(tmp_path, with_stop_time=False))
    assert out["checks"]["simulation_completed"]["status"] == acceptance.UNAVAILABLE
    assert out["status"] == acceptance.FAILED_ACCEPTANCE


def test_mdu_tstop_is_the_authority_and_disagreement_fails(tmp_path):
    r = make_run(tmp_path)
    (r["case_dir"] / "model.mdu").write_text("[time]\nTStop = 60.0\nMapInterval = 60.0\n")
    assert acceptance.check_simulation_completed(r["case_dir"], "model")["status"] == acceptance.PASS
    (r["case_dir"] / "model.mdu").write_text("[time]\nTStop = 6000.0\n")
    check = acceptance.check_simulation_completed(r["case_dir"], "model")
    assert check["status"] == acceptance.FAIL and "disagrees" in check["detail"]


def test_final_output_time_uses_the_files_own_units(tmp_path):
    path = tmp_path / "m_map.nc"
    ds = xr.Dataset(coords={"time": ("time", np.array([0.0, 30.0, 90.0]))})
    ds["time"].attrs["units"] = "minutes since 2001-01-01 00:00:00"
    ds.to_netcdf(path, encoding={"time": {"dtype": "float64"}})
    assert launcher.final_output_time_s(path) == pytest.approx(5400.0)
    assert launcher.final_output_time_s(tmp_path / "absent.nc") is None


def test_archive_failed_attempt_moves_output_and_never_overwrites(tmp_path):
    case, run = tmp_path / "run" / "case", tmp_path / "run"
    (case / "output").mkdir(parents=True)
    (case / "output" / "model.dia").write_text("** ERROR : boom\n")
    (case / "output" / "model_map.nc").write_bytes(b"partial")
    (run / "log.txt").write_text("solver log\n")
    target = launcher.archive_failed_attempt(case, run, 0, {"failure_reason": "solver reported failure"})
    assert (target / "failed_output" / "model.dia").read_text() == "** ERROR : boom\n"
    assert (target / "failed_output" / "model_map.nc").read_bytes() == b"partial"
    assert (target / "log.txt").read_text() == "solver log\n" and (run / "log.txt").exists()
    assert json.loads((target / "attempt_meta.json").read_text())["details"]["failure_reason"]
    assert not (case / "output").exists()
    (case / "output").mkdir()
    with pytest.raises(FileExistsError):
        launcher.archive_failed_attempt(case, run, 0)


def test_solver_version_comes_from_the_dia(tmp_path):
    from backend.m3_common.postprocess import _dia_solver_identity
    dia = tmp_path / "model.dia"
    dia.write_text("Program = D-Flow FM   # Program\nVersion = 1.2.184.Unknown  # Version\n")
    ident = _dia_solver_identity(dia)
    assert ident["solver_version"] == "D-Flow FM 1.2.184.Unknown" and ident["dia_present"]
    assert _dia_solver_identity(tmp_path / "absent.dia")["solver_version"] is None  # never assumed


def test_wall_time_parses_gnu_time_label(tmp_path):
    from backend.m3_common.postprocess import _solver_wall_seconds
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "resource_usage.txt").write_text(
        "\tElapsed (wall clock) time (h:mm:ss or m:ss): 1:02.50\n")
    assert _solver_wall_seconds(tmp_path) == pytest.approx(62.5)
    (tmp_path / "output" / "resource_usage.txt").write_text(
        "\tElapsed (wall clock) time (h:mm:ss or m:ss): 2:00:01\n")
    assert _solver_wall_seconds(tmp_path) == pytest.approx(7201.0)


def test_worker_spinup_comes_from_case_meta(tmp_path):
    from backend.m0_api.worker import _case_spinup_s
    (tmp_path / "case_meta.json").write_text(json.dumps({"spinup_s": 3600.0}))
    assert _case_spinup_s(tmp_path) == (3600.0, "case_meta.json")
    value, source = _case_spinup_s(tmp_path / "legacy")
    assert source.startswith("generator.SPINUP_S")


def test_pilot_registration_refuses_a_truncated_run(tmp_path, monkeypatch):
    from backend.m0_api import registry, run_registration
    data = tmp_path / "data"
    monkeypatch.setenv("SIH26_DATA_DIR", str(data))
    r = make_run(data, truncated=True)
    with pytest.raises(run_registration.RegistrationRefused, match="simulation_completed"):
        run_registration.register_existing_run("synth", r["run_id"], data_dir=data, pilot=True)
    registry.init_db()
    assert not (r["run_dir"] / "acceptance.json").exists()

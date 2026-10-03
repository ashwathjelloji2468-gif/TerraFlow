"""Feature 13: every acceptance check is separate; missing evidence is UNAVAILABLE, never PASS."""
from __future__ import annotations

import pytest

from backend.m3_dflowfm import acceptance as acc
from tests.m3_dflowfm.acceptance_fixtures import make_run


def _eval(r, **kw):
    return acc.evaluate_run(r["run_dir"], run_id=r["run_id"], scenario_id=r["scenario_id"], site_dir=r["site_dir"],
                            case_dir=r["case_dir"], model_stem="model", terrain_dir=r["terrain_dir"], **kw)


def test_fully_valid_synthetic_run_is_accepted(tmp_path):
    out = _eval(make_run(tmp_path))
    assert out["status"] == acc.ACCEPTED, out
    for name in acc.REQUIRED_CHECKS:
        assert out["checks"][name]["status"] == acc.PASS, name
    assert out["checks"]["mass_source"]["status"] == acc.UNAVAILABLE  # no approved tolerance; never PASS
    assert out["code_version"] and out["evaluated_at"].endswith("Z")


def test_poi_smoke_failure_is_failed_acceptance(tmp_path):
    out = _eval(make_run(tmp_path, poi_dry=True))
    assert out["status"] == acc.FAILED_ACCEPTANCE and out["failed_checks"] == ["poi_wetting"]
    assert out["checks"]["poi_wetting"]["evidence"]["per_poi"]["poi_b"]["wet"] is False


def test_solver_failure_rejected_regardless_of_outputs(tmp_path):
    out = _eval(make_run(tmp_path, dia_error=True))
    assert out["status"] == acc.FAILED_ACCEPTANCE and "solver_success" in out["failed_checks"]


@pytest.mark.parametrize("kw,check", [
    ({"outlet_dry": True}, "hydraulic_path"),
    ({"terrain_mismatch": True}, "terrain_compatibility"),
    ({"missing_output": "timeseries.csv"}, "required_outputs"),
    ({"identity_run_id": "other__delft3d"}, "run_identity"),
])
def test_each_failure_is_reported_separately(tmp_path, kw, check):
    out = _eval(make_run(tmp_path, **kw))
    assert out["status"] == acc.FAILED_ACCEPTANCE and check in out["failed_checks"]


def test_missing_evidence_is_unavailable_not_pass(tmp_path):
    r = make_run(tmp_path, with_run_meta=False)
    out = acc.evaluate_run(r["run_dir"], run_id=r["run_id"], scenario_id=r["scenario_id"], site_dir=r["site_dir"])
    assert out["status"] == acc.FAILED_ACCEPTANCE
    for name in ("solver_success", "postprocess_success", "run_identity", "terrain_compatibility",
                 "forcing_provenance", "hydraulic_path", "poi_wetting"):
        assert out["checks"][name]["status"] == acc.UNAVAILABLE, name
    assert set(out["unavailable_checks"]) >= {"solver_success", "poi_wetting"}

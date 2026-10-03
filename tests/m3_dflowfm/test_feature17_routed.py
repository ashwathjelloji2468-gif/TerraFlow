"""Feature 17: routed-discharge artifact -- extraction from (synthetic) M3 output, deterministic
validation, atomic replacement, source-run gating and the production routing gate.

All fixtures are tiny synthetic NetCDF / JSON files under pytest tmp dirs. Nothing here is a real
solver result and nothing claims a production routing section.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
import yaml
from shapely.geometry import LineString

from backend.m3_dflowfm import routed_discharge as rd
from backend.m3_dflowfm import routing_section
from backend.m3_dflowfm.section_discharge import write_section_discharge

SECTION = {"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 10.0]], "crs": "EPSG:32645"}
REPO = Path(__file__).resolve().parents[2]


def _m3_run(root: Path, run_id: str = "s001__delft3d", *, crs_epsg: int | None = 32645,
            acceptance: str | None = "ACCEPTED", run_class: str | None = None) -> Path:
    """One 100 m face, depth 0.5/1.0 m and speed 2 m/s after a 100 s spin-up (synthetic)."""
    run = root / run_id
    out = run / "case" / "output"
    out.mkdir(parents=True)
    meta = {"spinup_s": 100.0, **({"crs_epsg": crs_epsg} if crs_epsg else {})}
    (run / "case" / "case_meta.json").write_text(json.dumps(meta))
    run_meta = {"status": "postprocessed", **({"acceptance": {"status": acceptance}} if acceptance else {}),
                **({"run_class": run_class} if run_class else {})}
    (run / "run_meta.json").write_text(json.dumps(run_meta))
    times = np.datetime64("2001-01-01T00:00:00", "ns") + np.array([0, 100, 200, 300]) * np.timedelta64(1, "s")
    xr.Dataset({"mesh2d_node_x": ("n", [0.0, 100.0, 100.0, 0.0]), "mesh2d_node_y": ("n", [0.0, 0.0, 100.0, 100.0]),
                "mesh2d_face_nodes": (("f", "k"), [[0, 1, 2, 3]], {"start_index": 0}),
                "mesh2d_waterdepth": (("time", "f"), np.array([0.0, 0.5, 1.0, 0.5])[:, None]),
                "mesh2d_ucmag": (("time", "f"), np.array([0.0, 2.0, 2.0, 2.0])[:, None])},
               coords={"time": times}).to_netcdf(out / "model_map.nc")
    return run


def _write(path: Path, **overrides):
    kwargs = dict(site_id="synth", scenario_id="s001", source_run_id="s001__delft3d", t_s=[0, 10, 20],
                  q_m3s=[1.0, 4.0, 2.0], routing_method="controlled_fixture", section=SECTION,
                  provenance={"source_map": "synthetic-map"})
    kwargs.update(overrides)
    return rd.write_routed_discharge(path, **kwargs)


# --- extraction ---------------------------------------------------------------------------------
def test_extraction_writes_contract_location_with_hash_and_caveats(tmp_path):
    run = _m3_run(tmp_path)
    csv_path, sidecar = write_section_discharge(run, None, LineString([(50, -10), (50, 110)]), site_id="synth",
                                                scenario_id="s001", source_run_id="s001__delft3d",
                                                crs_epsg=32645, section_id="sec")
    assert sidecar == run / "routed_discharge" / "routed_discharge.json"  # contract §4.4 (E2)
    t, q, record, report = rd.validate_routed_discharge(sidecar, site_id="synth", scenario_id="s001",
                                                        expected_epsg=32645)
    np.testing.assert_allclose(t, [0.0, 100.0, 200.0])  # seconds since t0 (spin-up removed)
    np.testing.assert_allclose(q, [100.0, 200.0, 100.0])  # depth x speed x 100 m cut
    prov = record["provenance"]
    assert prov["timeseries_sha256"] == rd.sha256_file(csv_path) == report["timeseries_sha256"]
    assert prov["extraction_mode"] == "controlled" and prov["velocity_vector_available"] is False
    assert any("upper bound" in c for c in prov["caveats"])
    assert record["validation_status"] == "artifact_schema_checked"  # never production-validated here


def test_extraction_rejects_section_crs_that_differs_from_the_case(tmp_path):
    run = _m3_run(tmp_path, crs_epsg=32644)
    with pytest.raises(ValueError, match="does not match the M3 case CRS"):
        write_section_discharge(run, None, LineString([(50, -10), (50, 110)]), site_id="synth",
                                scenario_id="s001", source_run_id="s001__delft3d", crs_epsg=32645, section_id="sec")
    assert not (run / "routed_discharge").exists()


def test_unsafe_identifiers_are_refused(tmp_path):
    run = _m3_run(tmp_path)
    with pytest.raises(ValueError, match="not a valid"):
        write_section_discharge(run, None, LineString([(50, -10), (50, 110)]), site_id="synth",
                                scenario_id="../evil", source_run_id="s001__delft3d", crs_epsg=32645, section_id="x")


# --- validation ---------------------------------------------------------------------------------
def test_hash_mismatch_is_detected(tmp_path):
    csv_path, sidecar = _write(tmp_path / "rd")
    csv_path.write_text("t_s,q_m3s\n0,1\n10,999\n20,2\n")
    with pytest.raises(rd.RoutedDischargeInvalid, match="sha256"):
        rd.read_routed_discharge(sidecar)


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_blocked_or_failed_status_is_never_usable(tmp_path, status):
    _, sidecar = _write(tmp_path / "rd", validation_status=status)
    with pytest.raises(rd.RoutedDischargeInvalid, match="not usable"):
        rd.read_routed_discharge(sidecar)


def test_production_requires_validated_status(tmp_path):
    _, sidecar = _write(tmp_path / "rd")
    with pytest.raises(rd.RoutedDischargeInvalid, match="artifact_validated"):
        rd.validate_routed_discharge(sidecar, allowed_statuses=rd.PRODUCTION_STATUSES)


def test_wrong_units_fail_the_schema(tmp_path):
    _, sidecar = _write(tmp_path / "rd")
    record = json.loads(sidecar.read_text())
    record["flow_unit"] = "m3/h"
    sidecar.write_text(json.dumps(record))
    with pytest.raises(rd.RoutedDischargeInvalid, match="schema"):
        rd.read_routed_discharge(sidecar)


def test_section_geometry_and_crs_are_checked(tmp_path):
    with pytest.raises(rd.RoutedDischargeInvalid, match="zero length"):
        _write(tmp_path / "a", section={**SECTION, "coordinates": [[1.0, 1.0], [1.0, 1.0]]})
    with pytest.raises(rd.RoutedDischargeInvalid, match="EPSG"):
        _write(tmp_path / "b", section={**SECTION, "crs": "UTM45N"})
    _, sidecar = _write(tmp_path / "c")
    with pytest.raises(rd.RoutedDischargeInvalid, match="expected EPSG:32644"):
        rd.validate_routed_discharge(sidecar, expected_epsg=32644)


def test_provenance_must_name_its_source(tmp_path):
    with pytest.raises(rd.RoutedDischargeInvalid, match="source_map"):
        _write(tmp_path / "rd", provenance={"note": "no source"})


def test_series_order_finiteness_and_emptiness(tmp_path):
    with pytest.raises(rd.RoutedDischargeInvalid, match="increase"):
        _write(tmp_path / "a", t_s=[0, 20, 10], q_m3s=[1, 1, 1])
    with pytest.raises(rd.RoutedDischargeInvalid, match="finite"):
        _write(tmp_path / "b", t_s=[0, 10, 20], q_m3s=[1, float("nan"), 1])
    with pytest.raises(rd.RoutedDischargeInvalid, match="at least two"):
        _write(tmp_path / "c", t_s=[], q_m3s=[])


def test_time_coverage_window(tmp_path):
    _, sidecar = _write(tmp_path / "rd")
    rd.validate_routed_discharge(sidecar, window_s=(0.0, 20.0))
    with pytest.raises(rd.RoutedDischargeInvalid, match="window"):
        rd.validate_routed_discharge(sidecar, window_s=(0.0, 30.0))


@pytest.mark.parametrize("acceptance,run_class,expected", [
    ("ACCEPTED", None, "ACCEPTED"), ("FAILED_ACCEPTANCE", "pilot", "PILOT"),
    ("FAILED_ACCEPTANCE", None, "FAILED_ACCEPTANCE"), (None, None, "NOT_EVALUATED")])
def test_source_run_class_never_trusts_postprocessed_alone(acceptance, run_class, expected):
    meta = {"status": "postprocessed", **({"acceptance": {"status": acceptance}} if acceptance else {}),
            **({"run_class": run_class} if run_class else {})}
    assert rd.routed_source_class(meta) == expected


def test_source_run_must_exist_and_qualify(tmp_path):
    runs = tmp_path / "runs"
    _, sidecar = _write(tmp_path / "rd")
    with pytest.raises(rd.RoutedDischargeInvalid, match="no run_meta"):
        rd.validate_routed_discharge(sidecar, runs_dir=runs)
    _m3_run(runs, acceptance="FAILED_ACCEPTANCE")
    with pytest.raises(rd.RoutedDischargeInvalid, match="FAILED_ACCEPTANCE"):
        rd.validate_routed_discharge(sidecar, runs_dir=runs, source_run_statuses=("ACCEPTED", "PILOT"))


# --- atomic replacement -------------------------------------------------------------------------
def test_failed_rewrite_preserves_the_previous_valid_artifact(tmp_path, monkeypatch):
    target = tmp_path / "routed_discharge"
    csv_path, sidecar = _write(target)
    (target / "m4_inlet.json").write_text("{}")
    before = (csv_path.read_bytes(), sidecar.read_bytes())

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(rd, "_swap_in", boom)
    with pytest.raises(OSError):
        _write(target, q_m3s=[9.0, 9.0, 9.0])
    assert (csv_path.read_bytes(), sidecar.read_bytes()) == before
    rd.read_routed_discharge(sidecar)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".routed_discharge.")]  # no staging left


def test_invalid_rewrite_preserves_the_previous_artifact(tmp_path):
    target = tmp_path / "routed_discharge"
    csv_path, sidecar = _write(target)
    before = csv_path.read_bytes()
    with pytest.raises(rd.RoutedDischargeInvalid):
        _write(target, t_s=[0, 0, 1])
    assert csv_path.read_bytes() == before


def test_successful_rewrite_replaces_pair_and_keeps_siblings(tmp_path):
    target = tmp_path / "routed_discharge"
    _write(target)
    (target / "m4_inlet.json").write_text('{"keep": true}')
    csv_path, sidecar = _write(target, q_m3s=[5.0, 6.0, 7.0])
    _, q, _ = rd.read_routed_discharge(sidecar)
    np.testing.assert_array_equal(q, [5.0, 6.0, 7.0])
    assert json.loads((target / "m4_inlet.json").read_text()) == {"keep": True}


# --- campaign source gating ---------------------------------------------------------------------
def _cfg():
    from backend.shared.site_config import load_site_config
    return load_site_config("synth", sites_dir=REPO / "tests" / "fixtures" / "shared")


def test_failed_acceptance_m3_run_is_not_a_routed_source(tmp_path):
    from backend.campaign import _routed_discharge_from_delft3d
    data = tmp_path / "data"
    _m3_run(data / "synth" / "runs", "synth__s001__delft3d", acceptance="FAILED_ACCEPTANCE")
    with pytest.raises(ValueError, match="FAILED_ACCEPTANCE"):
        _routed_discharge_from_delft3d(_cfg(), "synth__s001", data, "synth__s001__sph", 300.0)


def test_production_routing_is_blocked_even_for_an_accepted_source(tmp_path):
    from backend.campaign import _routed_discharge_from_delft3d
    data = tmp_path / "data"
    run = _m3_run(data / "synth" / "runs", "synth__s001__delft3d", acceptance="ACCEPTED")
    with pytest.raises(ValueError, match="production routing BLOCKED"):
        _routed_discharge_from_delft3d(_cfg(), "synth__s001", data, "synth__s001__sph", 300.0, production=True,
                                       sites_dir=REPO / "tests" / "fixtures" / "shared")
    assert not (run / "routed_discharge").exists()


# --- routing-section config (E7) ----------------------------------------------------------------
def _section_file(config_dir: Path, *, approved: bool, method: str = routing_section.CONTROLLED_METHOD_ID,
                  half: bool = False):
    config_dir.mkdir(parents=True, exist_ok=True)
    approval = {"approved_by": "Reviewer" if approved or half else None,
                "approved_at": "2026-10-03" if approved else None}
    (config_dir / "synth.yaml").write_text(yaml.safe_dump({"site_id": "synth", "sections": [{
        "id": "synth_inflow", "purpose": "m3_to_m4_inflow", "extraction_method": method, "source": "src_000",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [100, 0]], "crs": "EPSG:32645"},
        "approval": approval}]}))


def _status(tmp_path, config_dir, checklist=None):
    return routing_section.production_status("synth", data_dir=tmp_path / "data",
                                             sites_dir=REPO / "tests" / "fixtures" / "shared",
                                             config_dir=config_dir, checklist_path=checklist)


def test_routing_blocked_without_a_reviewed_file(tmp_path):
    out = _status(tmp_path, tmp_path / "rs")
    assert out["status"] == routing_section.BLOCKED
    assert any("no reviewed routing-section file" in r for r in out["reasons"])


def test_file_presence_alone_never_approves(tmp_path):
    _section_file(tmp_path / "rs", approved=False)
    out = _status(tmp_path, tmp_path / "rs")
    assert out["status"] == routing_section.BLOCKED
    assert any("no named approval" in r for r in out["reasons"])
    assert any("decision S2 deferred" in r for r in out["reasons"])


def test_half_approval_is_malformed(tmp_path):
    _section_file(tmp_path / "rs", approved=False, half=True)
    with pytest.raises(routing_section.RoutingSectionError, match="both approved_by and approved_at"):
        routing_section.load("synth", config_dir=tmp_path / "rs")


def test_ready_only_when_every_condition_holds(tmp_path, monkeypatch):
    """Determinism check of the rule itself: even an approved file + approved method stays BLOCKED
    until the checklist item is READY. (No real method is approved; the constant is patched here.)"""
    _section_file(tmp_path / "rs", approved=True, method="hypothetical_method")
    monkeypatch.setattr(routing_section, "APPROVED_PRODUCTION_METHODS", ("hypothetical_method",))
    checklist = tmp_path / "checklist.json"
    checklist.write_text(json.dumps({"shared_gates": [], "sites": {"synth": {
        "production_gate": "BLOCKED", "inputs": [{"key": "m3_to_m4_routed_section", "status": "PLACEHOLDER"}]}}}))
    out = _status(tmp_path, tmp_path / "rs", checklist)
    assert out["status"] == routing_section.BLOCKED and len(out["reasons"]) == 1
    checklist.write_text(json.dumps({"shared_gates": [], "sites": {"synth": {
        "production_gate": "READY", "inputs": [{"key": "m3_to_m4_routed_section", "status": "READY"}]}}}))
    first, second = _status(tmp_path, tmp_path / "rs", checklist), _status(tmp_path, tmp_path / "rs", checklist)
    assert first["status"] == routing_section.READY and first["reasons"] == second["reasons"] == []


def test_teesta_production_routing_is_blocked():
    out = routing_section.production_status("teesta", data_dir=REPO / "data")
    assert out["status"] == routing_section.BLOCKED
    assert routing_section.APPROVED_PRODUCTION_METHODS == ()
    assert not (REPO / "config" / "routing_sections" / "teesta.yaml").exists()

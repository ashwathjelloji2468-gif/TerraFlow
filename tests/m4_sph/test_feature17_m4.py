"""Feature 17: M4 consumes ONLY a validated M3 routed-discharge artifact, records which one, and the
SPH campaign never overwrites a valid run (synthetic fixtures; no GenCase / solver runs here)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.m3_dflowfm import routed_discharge as rd
from backend.m4_sph.generator import build_nearfield_case
from backend.m4_sph.settings import load_sph_settings

SECTION = {"type": "LineString", "coordinates": [[0, 0], [10, 10]], "crs": "EPSG:32645"}
T, Q = [0, 600, 1200, 1800], [20.0, 80.0, 40.0, 10.0]


def _artifact(data_dir, *, acceptance="ACCEPTED", status="artifact_schema_checked", t=T, crs="EPSG:32645"):
    source = data_dir / "synth" / "runs" / "synth_s001__delft3d"
    source.mkdir(parents=True, exist_ok=True)
    (source / "run_meta.json").write_text(json.dumps({"status": "postprocessed", "acceptance": {"status": acceptance}}))
    _, sidecar = rd.write_routed_discharge(
        source / "routed_discharge", site_id="synth", scenario_id="synth_s001", source_run_id="synth_s001__delft3d",
        t_s=t, q_m3s=Q[:len(t)], routing_method="controlled_fixture", section={**SECTION, "crs": crs},
        provenance={"source_map": "synthetic-map"}, validation_status=status)
    return sidecar


def _build(env, sidecar, **kwargs):
    terrain, params, sites = env
    return build_nearfield_case("synth", "synth_s001", params, load_sph_settings(dp_m=2.0),
                                data_dir=terrain.parent.parent, sites_dir=sites, routed_discharge_path=sidecar, **kwargs)


@pytest.fixture
def env(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir):
    return synth_terrain_dir, synth_hydrograph_params, synth_sites_dir


def test_inlet_velocity_conserves_routed_mass_flux_and_records_the_artifact(env):
    sidecar = _artifact(env[0].parent.parent)
    spec, meta = _build(env, sidecar)
    zone, area = spec.inout_zones[0], meta["inlet"]["area_m2"]
    tau = np.asarray([v.time_s for v in zone.velocity_times]) + meta["t_start_s"]
    np.testing.assert_allclose(np.asarray([v.v_ms for v in zone.velocity_times]) * area, np.interp(tau, T, Q))
    art = meta["provenance"]["routed_discharge_artifact"]
    assert art["manifest"] == str(sidecar) and art["manifest_sha256"] == rd.sha256_file(sidecar)
    assert art["timeseries_sha256"] == json.loads(sidecar.read_text())["provenance"]["timeseries_sha256"]
    assert meta["provenance"]["hydrograph_method"] == "m3_routed_discharge" and meta["production"] is False


def test_tampered_series_is_rejected_with_no_fallback(env):
    sidecar = _artifact(env[0].parent.parent)
    (sidecar.parent / "timeseries.csv").write_text("t_s,q_m3s\n0,1\n1800,1\n")
    with pytest.raises(rd.RoutedDischargeInvalid, match="sha256"):
        _build(env, sidecar)


def test_failed_acceptance_source_is_rejected(env):
    with pytest.raises(rd.RoutedDischargeInvalid, match="FAILED_ACCEPTANCE"):
        _build(env, _artifact(env[0].parent.parent, acceptance="FAILED_ACCEPTANCE"))


def test_missing_source_run_is_rejected(env):
    sidecar = _artifact(env[0].parent.parent)
    (env[0].parent.parent / "synth" / "runs" / "synth_s001__delft3d" / "run_meta.json").unlink()
    with pytest.raises(rd.RoutedDischargeInvalid, match="no run_meta"):
        _build(env, sidecar)


def test_crs_must_match_the_site(env):
    with pytest.raises(rd.RoutedDischargeInvalid, match="expected EPSG"):
        _build(env, _artifact(env[0].parent.parent, crs="EPSG:32644"))


def test_series_must_cover_the_configured_window(env):
    with pytest.raises(rd.RoutedDischargeInvalid, match="window"):
        _build(env, _artifact(env[0].parent.parent, t=[60, 600, 1200, 1800]))  # starts after t_start_s = 0


def test_production_case_requires_a_validated_artifact(env):
    with pytest.raises(rd.RoutedDischargeInvalid, match="artifact_validated"):
        _build(env, _artifact(env[0].parent.parent), production=True)


# --- SPH campaign (fake generator, as tests/test_campaign.py) ----------------------------------
from tests.test_campaign import (_install_fake_generator, _synth_raw_with_sph_scenarios,  # noqa: E402
                                 _write_design, _write_site, conn, data_dir, sites_dir)  # noqa: F401


def test_existing_postprocessed_sph_run_is_preserved(conn, data_dir, sites_dir, monkeypatch):
    from backend.campaign import run_sph_campaign
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])
    run_dir = data_dir / "synth" / "runs" / "synth__s001__sph"
    run_dir.mkdir(parents=True)
    (run_dir / "run_meta.json").write_text('{"status": "postprocessed", "valid": true}')
    with conn:
        conn.execute("INSERT INTO scenarios (scenario_id, site_id, kind, params_json, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("synth__s001", "synth", "design", "{}", "2026-10-03T00:00:00Z"))
        conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json) VALUES (?, ?, 'sph', "
                     "'postprocessed', ?, ?)", ("synth__s001__sph", "synth__s001", str(run_dir), '{"valid": true}'))
    _, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)
    assert results[0].status == "preserved"
    row = conn.execute("SELECT status, meta_json FROM runs WHERE run_id = 'synth__s001__sph'").fetchone()
    assert row[0] == "postprocessed" and json.loads(row[1]) == {"valid": True}
    assert json.loads((run_dir / "run_meta.json").read_text())["valid"] is True


def test_failed_sph_run_is_archived_before_rebuild(conn, data_dir, sites_dir, monkeypatch):
    from backend.campaign import run_sph_campaign
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])
    run_dir = data_dir / "synth" / "runs" / "synth__s001__sph"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "raw" / "log.txt").write_text("failed attempt evidence")
    with conn:
        conn.execute("INSERT INTO scenarios (scenario_id, site_id, kind, params_json, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("synth__s001", "synth", "design", "{}", "2026-10-03T00:00:00Z"))
        conn.execute("INSERT INTO runs (run_id, scenario_id, model, status, run_dir, meta_json) VALUES (?, ?, 'sph', "
                     "'failed', ?, '{}')", ("synth__s001__sph", "synth__s001", str(run_dir)))
    _, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)
    assert results[0].status == "queued"
    assert (run_dir / "archive" / "rebuild_01" / "raw" / "log.txt").read_text() == "failed attempt evidence"


def test_production_sph_campaign_is_blocked_and_queues_nothing(conn, data_dir, sites_dir, monkeypatch):
    from backend.campaign import run_sph_campaign
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])
    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir, production=True)
    assert job_id is None and results[0].status == "blocked" and "production gate (sph) BLOCKED" in results[0].reason
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

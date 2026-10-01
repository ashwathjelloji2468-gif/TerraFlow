"""Feature 5 phase 1: the read-only D-Flow FM preflight."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from backend.m3_dflowfm import preflight
from backend.m3_dflowfm.preflight import BLOCKED, PASS, WARNING, run_preflight
from backend.shared.site_config import load_site_config

REPO = Path(__file__).resolve().parents[2]


def _by_name(report: dict) -> dict:
    return {c["name"]: c for c in report["checks"]}


def _snapshot(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture
def ready_site(synth_terrain_dir, synth_m3_sites_dir, tmp_path):
    """A synthetic site with terrain, a real Feature 3 breach stage and a design that records the
    breach_params sha256. Returns (data_dir, sites_dir)."""
    from backend.m2_breach.reference import run_breach_stage

    data_dir = synth_terrain_dir.parent.parent
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = load_site_config("synth", sites_dir=synth_m3_sites_dir)
    bp = run_breach_stage(cfg, data_dir=data_dir)
    design = {"site_id": "synth", "model": "delft3d", "scenarios": [{"scenario_id": "synth__s001"}],
              "extra": [], "provenance": {"breach_params_sha256": hashlib.sha256(bp.read_bytes()).hexdigest()}}
    (data_dir / "synth" / "design").mkdir(parents=True)
    (data_dir / "synth" / "design" / "scenario_design.json").write_text(json.dumps(design))
    return data_dir, synth_m3_sites_dir


def _kernel(tmp_path: Path) -> Path:
    k = tmp_path / "bin" / "run_dflowfm.sh"
    k.parent.mkdir(parents=True, exist_ok=True)
    k.write_text("#!/bin/sh\nexit 0\n")
    k.chmod(0o755)
    return k


def test_teesta_null_base_flow_is_blocked_and_no_value_is_supplied(tmp_path):
    report = run_preflight("teesta", data_dir=tmp_path, kernel=tmp_path / "none")
    bf = _by_name(report)["base_flow"]
    assert bf["status"] == BLOCKED
    assert "no default is supplied" in bf["detail"]
    assert "value" not in bf["evidence"]
    for forbidden in ("500", "60", "glofas"):
        assert forbidden not in (bf["detail"] + json.dumps(bf["evidence"])).lower()
    assert report["overall"] == BLOCKED


def test_placeholder_base_flow_is_a_warning_with_its_source(ready_site, tmp_path):
    data_dir, sites_dir = ready_site
    bf = _by_name(run_preflight("synth", data_dir=data_dir, sites_dir=sites_dir,
                                kernel=_kernel(tmp_path)))["base_flow"]
    assert bf["status"] == WARNING
    assert bf["evidence"] == {"value": 12.0, "unit": "m^3/s", "status": "placeholder",
                              "source": "Synthetic test fixture"}


def test_ready_synthetic_site_passes_site_checks(ready_site, tmp_path):
    data_dir, sites_dir = ready_site
    checks = _by_name(run_preflight("synth", data_dir=data_dir, sites_dir=sites_dir,
                                    kernel=_kernel(tmp_path), min_free_gb=0.0))
    for name in ("kernel", "site_config", "terrain", "breach", "scenario_design", "disk"):
        assert checks[name]["status"] == PASS, checks[name]
    assert checks["scenario_design"]["evidence"]["breach_params_sha256_matches"] is True


def test_stale_design_sha_is_blocked(ready_site, tmp_path):
    data_dir, sites_dir = ready_site
    path = data_dir / "synth" / "design" / "scenario_design.json"
    design = json.loads(path.read_text())
    design["provenance"]["breach_params_sha256"] = "0" * 64
    path.write_text(json.dumps(design))
    check = _by_name(run_preflight("synth", data_dir=data_dir, sites_dir=sites_dir))["scenario_design"]
    assert check["status"] == BLOCKED and "sha256 mismatch" in check["detail"]


def test_missing_terrain_breach_and_kernel_are_blocked(tmp_path, synth_m3_sites_dir):
    checks = _by_name(run_preflight("synth", data_dir=tmp_path / "empty", sites_dir=synth_m3_sites_dir,
                                    kernel=tmp_path / "missing.sh"))
    assert checks["kernel"]["status"] == BLOCKED
    assert checks["terrain"]["status"] == BLOCKED
    assert set(checks["terrain"]["evidence"]["missing"]) == set(preflight.TERRAIN_FILES)
    assert checks["breach"]["status"] == BLOCKED
    assert checks["scenario_design"]["status"] == WARNING


def test_non_executable_kernel_is_blocked(tmp_path):
    k = tmp_path / "run_dflowfm.sh"
    k.write_text("")
    k.chmod(0o644)
    assert preflight.check_kernel(k).status == BLOCKED


def test_kernel_env_override_is_used(tmp_path, monkeypatch):
    k = _kernel(tmp_path)
    monkeypatch.setenv("SIH26_DFLOWFM_KERNEL", str(k))
    check = preflight.check_kernel()
    assert check.status == PASS and check.evidence["origin"] == "SIH26_DFLOWFM_KERNEL"


def test_missing_time_binary_is_blocked(tmp_path):
    assert preflight.check_time_binary(tmp_path / "time").status == BLOCKED


def test_disk_threshold_comes_from_the_caller(tmp_path):
    assert preflight.check_disk(tmp_path, None).status == WARNING
    assert preflight.check_disk(tmp_path, 0.0).status == PASS
    assert preflight.check_disk(tmp_path, 1e12).status == BLOCKED


def test_apple_silicon_is_blocked(monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "arm64")
    assert preflight.check_platform().status == BLOCKED


def test_preflight_never_modifies_files(ready_site, tmp_path):
    data_dir, sites_dir = ready_site
    kernel = _kernel(tmp_path)
    before = _snapshot(data_dir, sites_dir, REPO / "sites")
    run_preflight("synth", data_dir=data_dir, sites_dir=sites_dir, kernel=kernel)
    run_preflight("teesta", data_dir=data_dir)
    assert _snapshot(data_dir, sites_dir, REPO / "sites") == before


def test_cli_reports_json_and_exit_status(tmp_path):
    env = {**os.environ, "SIH26_DATA_DIR": str(tmp_path)}
    env.pop("SIH26_DFLOWFM_KERNEL", None)
    proc = subprocess.run([sys.executable, str(REPO / "scripts/dflowfm_preflight.py"), "teesta", "--json"],
                          capture_output=True, text=True, env=env, cwd=REPO)
    assert proc.returncode == 2, proc.stderr
    report = json.loads(proc.stdout)
    assert report["data_dir"] == str(tmp_path)
    assert _by_name(report)["base_flow"]["status"] == BLOCKED
    assert list(tmp_path.iterdir()) == []

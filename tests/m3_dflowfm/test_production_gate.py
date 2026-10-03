"""Feature 13 phase 1: the production gate never accepts placeholders/nulls/missing artifacts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from backend.m3_dflowfm import preflight, production_gate as gate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "shared" / "synth.yaml"
FILL = {"m^3": 1.0e6, "iso8601": "2020-01-02", "m^3/s": 5.0}


def _sourced(node):
    """A fully *sourced* synthetic config (test fixture only; values are synthetic, not real data)."""
    if isinstance(node, dict):
        if {"value", "status", "source"} <= set(node):
            node = dict(node)
            if node["value"] is None:
                node["value"] = FILL.get(node.get("unit"), 1.0)
            node.update(status="sourced", source="synthetic test fixture (sourced for the gate test)")
            return node
        return {k: _sourced(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_sourced(v) for v in node]
    return node


def _site(tmp_path, *, sourced=True, base_flow="sourced", pois=True, terrain=True):
    raw = yaml.safe_load(FIXTURE.read_text())
    raw = _sourced(raw) if sourced else copy.deepcopy(raw)
    bf = {"sourced": {"value": 5.0, "unit": "m^3/s", "source": "synthetic", "status": "sourced"},
          "placeholder": {"value": 5.0, "unit": "m^3/s", "source": "guess", "status": "placeholder"},
          "null": {"value": None, "unit": "m^3/s", "source": "NOT SOURCED", "status": "placeholder"}}[base_flow]
    raw["domains"]["far_field"]["inflow"]["base_flow"] = bf
    if not pois:
        raw["points_of_interest"] = []
    sites, data = tmp_path / "sites", tmp_path / "data"
    sites.mkdir()
    (sites / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    if terrain:
        t = data / "synth" / "terrain"
        t.mkdir(parents=True)
        for name in gate.TERRAIN_FILES:
            (t / name).write_text("x")
    checklist = tmp_path / "checklist.json"
    checklist.write_text(json.dumps({"shared_gates": [
        {"key": "production_run_budget", "status": "READY", "source": "test"},
        {"key": "m3_frozen_pilot_reproduction", "status": "CONTROLLED PASS", "source": "test"}], "sites": {}}))
    return dict(data_dir=data, sites_dir=sites, checklist_path=checklist)


def test_valid_synthetic_production_config_is_ready(tmp_path):
    r = gate.evaluate("synth", **_site(tmp_path))
    assert r["verdict"] == gate.READY, r["blocking"]
    assert any(i["status"] == gate.CONTROLLED_PASS and not i["blocking"] for i in r["items"])


def test_placeholder_base_flow_is_blocked_not_warning(tmp_path):
    r = gate.evaluate("synth", **_site(tmp_path, base_flow="placeholder"))
    assert r["verdict"] == gate.BLOCKED and "domains.far_field.inflow.base_flow" in r["blocking"]
    item = next(i for i in r["items"] if i["key"] == "domains.far_field.inflow.base_flow")
    assert item["status"] == gate.PLACEHOLDER and item["blocking"] is True


def test_null_base_flow_is_blocked(tmp_path):
    r = gate.evaluate("synth", **_site(tmp_path, base_flow="null"))
    item = next(i for i in r["items"] if i["key"] == "domains.far_field.inflow.base_flow")
    assert item["status"] == gate.BLOCKED and r["verdict"] == gate.BLOCKED


def test_missing_pois_blocked(tmp_path):
    r = gate.evaluate("synth", **_site(tmp_path, pois=False))
    assert "points_of_interest" in r["blocking"] and r["verdict"] == gate.BLOCKED


def test_missing_terrain_blocked(tmp_path):
    r = gate.evaluate("synth", **_site(tmp_path, terrain=False))
    assert "terrain_artifacts" in r["blocking"]


def test_unknown_site_is_honest(tmp_path):
    r = gate.evaluate("nosuch_site", data_dir=tmp_path, sites_dir=tmp_path)
    assert r["verdict"] == gate.BLOCKED and r["config_loaded"] is False and r["blocking"] == ["site_config"]
    assert str(tmp_path) not in json.dumps(r)


def test_unavailable_shared_gate_blocks(tmp_path):
    kw = _site(tmp_path)
    Path(kw["checklist_path"]).write_text(json.dumps({"shared_gates": [
        {"key": "production_run_budget", "status": "UNAVAILABLE — PRODUCTION RUN BLOCKED"}]}))
    assert gate.evaluate("synth", **kw)["blocking"] == ["production_run_budget"]


def test_teesta_is_blocked_with_known_inputs():
    r = gate.evaluate("teesta")
    assert r["verdict"] == gate.BLOCKED
    for key in ("domains.far_field.inflow.base_flow", "dams[1].trigger.value", "points_of_interest[0].location",
                "production_run_budget", "site_production_gate"):
        assert key in r["blocking"], key


def test_preflight_production_mode_blocks_placeholder_base_flow(tmp_path):
    kw = _site(tmp_path, base_flow="placeholder")
    rep = preflight.run_preflight("synth", data_dir=kw["data_dir"], sites_dir=kw["sites_dir"], production=True)
    bf = next(c for c in rep["checks"] if c["name"] == "base_flow")
    assert bf["status"] == preflight.BLOCKED and rep["overall"] == preflight.BLOCKED
    plain = preflight.run_preflight("synth", data_dir=kw["data_dir"], sites_dir=kw["sites_dir"])
    assert next(c for c in plain["checks"] if c["name"] == "base_flow")["status"] == preflight.WARNING

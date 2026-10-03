"""Feature 14: evidence-tracked Teesta input resolution. Nothing is approved by tooling; quotes must be
verbatim in docs/data_sources.md; only approved entries can ever reach the site config."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from backend.m3_dflowfm import production_gate
from backend.shared import input_resolution as ir

ROOT = Path(__file__).resolve().parents[2]


def _copy(tmp_path):
    sites, reg = tmp_path / "sites", tmp_path / "reg"
    sites.mkdir()
    reg.mkdir()
    shutil.copy(ROOT / "sites/teesta.yaml", sites / "teesta.yaml")
    shutil.copy(ROOT / "config/input_resolution/teesta.yaml", reg / "teesta.yaml")
    return sites, reg


def _edit_register(reg, fn):
    path = reg / "teesta.yaml"
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def test_committed_teesta_register_is_valid_and_unapproved():
    assert ir.validate("teesta") == []
    reg = ir.load_register("teesta")
    assert reg["entries"] and all(ir.entry_status(e) == ir.CANDIDATE for e in reg["entries"])  # nothing self-approved


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["entries"][0].update(quote="62 m maximum height"), "quote not found verbatim"),
    (lambda d: d["entries"][0].update(source_id="src_999"), "not found in docs/data_sources.md"),
    (lambda d: d["entries"][0].update(unit="ft"), "unit"),
    (lambda d: d["entries"][0].update(target="dams[1].breach_inputs.nonexistent"), "not a sourced value"),
    (lambda d: d["entries"][0].update(basis="guess"), "basis"),
    (lambda d: d["entries"][0].update(approval={"approved_by": "x", "approved_at": None}), "approval needs both"),
])
def test_invalid_registers_are_refused(tmp_path, mutate, expected):
    sites, reg = _copy(tmp_path)
    _edit_register(reg, mutate)
    problems = ir.validate("teesta", sites_dir=sites, register_dir=reg)
    assert any(expected in p for p in problems), problems
    with pytest.raises(ir.RegisterError):
        ir.apply("teesta", sites_dir=sites, register_dir=reg)


def test_candidates_are_never_applied(tmp_path):
    sites, reg = _copy(tmp_path)
    before = (sites / "teesta.yaml").read_text()
    out = ir.apply("teesta", write=True, sites_dir=sites, register_dir=reg)
    assert out["applied"] == [] and out["written"] is False and len(out["skipped"]) == 3
    assert (sites / "teesta.yaml").read_text() == before


def test_approved_entry_is_applied_with_citation_and_comments_kept(tmp_path):
    sites, reg = _copy(tmp_path)
    _edit_register(reg, lambda d: d["entries"][0].update(approval={"approved_by": "Test Reviewer", "approved_at": "2026-10-03"}))
    dry = ir.apply("teesta", sites_dir=sites, register_dir=reg)
    assert dry["applied"] == ["dams[1].breach_inputs.dam_height"] and dry["written"] is False
    out = ir.apply("teesta", write=True, sites_dir=sites, register_dir=reg)
    assert out["written"] is True
    text = (sites / "teesta.yaml").read_text()
    assert "# sites/teesta.yaml" in text  # comments preserved
    cfg = yaml.safe_load(text)
    sv = cfg["dams"][1]["breach_inputs"]["dam_height"]
    assert sv["value"] == 60 and sv["status"] == "sourced"
    assert "src_043" in sv["source"] and "Test Reviewer" in sv["source"]
    assert cfg["dams"][0]["breach_inputs"]["dam_height"]["status"] == "placeholder"  # other dam untouched
    gate = production_gate.evaluate("teesta", sites_dir=sites)
    item = next(i for i in gate["items"] if i["key"] == "dams[1].breach_inputs.dam_height")
    assert item["status"] == production_gate.READY and gate["verdict"] == production_gate.BLOCKED  # still blocked overall


def test_report_separates_candidate_unresolved_and_evidence_only():
    rep = ir.report("teesta")
    assert rep["gate_verdict"] == "BLOCKED"
    assert rep["counts"][ir.APPROVED] == 0 and rep["counts"][ir.CANDIDATE] == 3
    rows = {r["key"]: r for r in rep["rows"]}
    bf = rows["domains.far_field.inflow.base_flow"]
    assert bf["resolution"] == ir.UNRESOLVED and bf["proposed_value"] is None
    assert bf["evidence_only"][0]["source_id"] == "src_075" and bf["required_artifact"]
    assert rows["dams[1].initial_water_level"]["resolution"] == ir.UNRESOLVED
    assert rows["dams[1].breach_inputs.dam_type"]["basis"] == "mapping"
    assert rows["dams[0].breach_inputs.water_volume_above_invert"]["basis"] == "interpretation"
    assert "production_run_budget" in rep["non_config_blocking"]


def test_committed_report_matches_generated():
    assert (ROOT / "docs/teesta_input_resolution.md").read_text() == ir.report_markdown(ir.report("teesta"))


def test_teesta_config_itself_unchanged_by_feature14():
    cfg = yaml.safe_load((ROOT / "sites/teesta.yaml").read_text())
    assert cfg["dams"][1]["breach_inputs"]["dam_height"]["status"] == "placeholder"
    assert cfg["domains"]["far_field"]["inflow"]["base_flow"]["value"] is None

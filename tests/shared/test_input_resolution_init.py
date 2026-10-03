"""Feature 16 P3: empty, deterministic input-resolution register for any site. Synthetic configs
and temporary register directories only; the repo register folder is never written."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from backend.shared import input_resolution as ir
from backend.shared import input_resolution_init as init

REPO = Path(__file__).resolve().parents[2]
SID = "kosi_reach"
TEESTA_WORDS = ("teesta", "lhonak", "chungthang", "src_043", "src_048", "south_lhonak")


@pytest.fixture
def world(tmp_path):
    sites, reg = tmp_path / "sites", tmp_path / "register"
    sites.mkdir()
    cfg = yaml.safe_load((REPO / "sites" / "synth_engdam.yaml").read_text(encoding="utf-8"))
    cfg["site"]["id"] = SID
    cfg["dams"][0]["breach_inputs"]["dam_height"].update(status="placeholder", source="synthetic placeholder")
    (sites / f"{SID}.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return {"sites": sites, "reg": reg, "data": tmp_path / "data"}


def _init(w, sid=SID, **kw):
    return init.init_register(sid, data_dir=w["data"], sites_dir=w["sites"], register_dir=w["reg"], **kw)


def test_new_site_gets_an_empty_valid_register(world):
    out = _init(world)
    assert out["status"] == "created" and out["problems"] == []
    assert out["register"] == f"<register_dir>/{SID}.yaml"
    reg = yaml.safe_load((world["reg"] / f"{SID}.yaml").read_text(encoding="utf-8"))
    assert reg == {"site_id": SID, "entries": [], "evidence_only": []}  # existing format, nothing else
    assert ir.validate(SID, sites_dir=world["sites"], register_dir=world["reg"]) == []


def test_every_blocking_input_is_unresolved_and_nothing_invented(world):
    out = _init(world)
    assert "dams[0].breach_inputs.dam_height" in out["pending"]
    rep = ir.report(SID, data_dir=world["data"], sites_dir=world["sites"], register_dir=world["reg"])
    assert rep["counts"]["APPROVED"] == 0 and rep["counts"]["CANDIDATE"] == 0
    assert {r["resolution"] for r in rep["rows"]} == {"UNRESOLVED"}
    assert all(r["proposed_value"] is None and r["source_id"] is None for r in rep["rows"])
    assert sorted(out["pending"]) == sorted(r["key"] for r in rep["rows"])  # header list == report view
    text = (world["reg"] / f"{SID}.yaml").read_text(encoding="utf-8")
    assert "proposed_value" not in text and "approved_by" not in text and "source_id:" not in text


def test_no_teesta_leak(world):
    _init(world)
    text = (world["reg"] / f"{SID}.yaml").read_text(encoding="utf-8").lower()
    for word in TEESTA_WORDS:
        assert word not in text, word


def test_deterministic_bytes(world, tmp_path):
    _init(world)
    first = (world["reg"] / f"{SID}.yaml").read_bytes()
    other = tmp_path / "register2"
    init.init_register(SID, data_dir=world["data"], sites_dir=world["sites"], register_dir=other)
    assert (other / f"{SID}.yaml").read_bytes() == first
    assert str(tmp_path) not in first.decode()  # no absolute paths
    dry = _init(world | {"reg": tmp_path / "register3"}, dry_run=True)
    assert dry["status"] == "dry_run" and dry["text"].encode() == first
    assert not (tmp_path / "register3").exists()  # dry run writes nothing


def test_second_run_is_a_noop_preserving_decisions(world):
    _init(world)
    path = world["reg"] / f"{SID}.yaml"
    edited = path.read_text(encoding="utf-8").replace("entries: []", (
        "entries:\n- target: dams[0].breach_inputs.dam_height\n  proposed_value: 25\n  unit: m\n"
        "  source_id: src_000\n  quote: reviewer text\n  basis: direct\n"
        "  approval: {approved_by: Reviewer A, approved_at: '2026-10-03'}\n"
        "  decision: {state: rejected, reviewer: Reviewer B, decided_at: '2026-10-03', rationale: r}"))
    path.write_text(edited, encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = _init(world)
    assert out["status"] == "exists" and "untouched" in out["message"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_existing_teesta_register_untouched():
    path = ir.REGISTER_DIR / "teesta.yaml"
    before = path.read_bytes()
    out = init.init_register("teesta")
    assert out["status"] == "exists" and out["register"] == "config/input_resolution/teesta.yaml"
    assert path.read_bytes() == before


def test_symlink_at_target_is_not_followed(world, tmp_path):
    victim = tmp_path / "victim.yaml"
    victim.write_text("keep\n")
    world["reg"].mkdir()
    (world["reg"] / f"{SID}.yaml").symlink_to(victim)
    assert _init(world)["status"] == "exists" and victim.read_text() == "keep\n"


@pytest.mark.parametrize("bad", ["../teesta", "teesta/../x", "/etc/passwd", "Teesta", "ab", "a" * 40,
                                 "x.yaml", "kosi reach", "", None, "1site", "..", "site\x00x"])
def test_unsafe_site_id_rejected(world, bad):
    with pytest.raises(init.InitError):
        _init(world, sid=bad)
    assert not world["reg"].exists()


def test_cannot_write_another_sites_register(world):
    """A config whose site.id differs from the requested id is refused."""
    (world["sites"] / "other_site.yaml").write_text((world["sites"] / f"{SID}.yaml").read_text())
    with pytest.raises(init.InitError):
        _init(world, sid="other_site")
    assert not world["reg"].exists()


def test_unknown_site_refused(world):
    with pytest.raises(init.InitError, match="no loadable site config"):
        _init(world, sid="no_such_site")
    assert not world["reg"].exists()


def test_onboarded_site_supported(tmp_path):
    data = tmp_path / "data"
    cfg_dir = data / SID / "config"
    cfg_dir.mkdir(parents=True)
    cfg = yaml.safe_load((REPO / "sites" / "demo_valley.yaml").read_text(encoding="utf-8"))
    cfg["site"]["id"] = SID
    (cfg_dir / f"{SID}.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    out = init.init_register(SID, data_dir=data, register_dir=tmp_path / "reg")
    assert out["status"] == "created" and out["problems"] == []


def test_cli_deterministic(world, capsys):
    args = [SID, "--sites-dir", str(world["sites"]), "--data-dir", str(world["data"]), "--register-dir", str(world["reg"])]
    assert init.main(args + ["--dry-run"]) == 0
    a = capsys.readouterr().out
    assert init.main(args + ["--dry-run"]) == 0
    assert capsys.readouterr().out == a
    assert init.main(args) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "created" and "text" not in created
    assert init.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "exists"
    assert init.main(["../x", "--register-dir", str(world["reg"])]) == 2

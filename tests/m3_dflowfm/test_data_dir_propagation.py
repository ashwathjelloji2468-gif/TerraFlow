"""Feature 5 phase 1: the D-Flow FM generator and campaign honour SIH26_DATA_DIR / data_dir."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from backend.m3_dflowfm import generator


def test_generator_has_no_import_time_data_dir():
    assert not hasattr(generator, "DATA_DIR")


def test_build_case_defaults_to_sih26_data_dir(synth_terrain_dir, synth_m3_sites_dir,
                                               synth_hydrograph_params, monkeypatch):
    data_dir = synth_terrain_dir.parent.parent
    monkeypatch.setenv("SIH26_DATA_DIR", str(data_dir))
    case, meta = generator.build_case("synth", "s001", synth_hydrograph_params,
                                      sites_dir=synth_m3_sites_dir, stop_s=3600)
    assert case == data_dir / "synth" / "runs" / "s001" / "dflowfm"
    assert (case / "model.mdu").is_file()
    assert meta["mesh"]["face_count"] > 0


def test_build_case_without_terrain_under_sih26_data_dir_fails(synth_m3_sites_dir, synth_hydrograph_params,
                                                               tmp_path, monkeypatch):
    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "elsewhere"))
    with pytest.raises(Exception):
        generator.build_case("synth", "s001", synth_hydrograph_params, sites_dir=synth_m3_sites_dir,
                             stop_s=3600)
    assert not (Path(__file__).resolve().parents[2] / "data" / "synth" / "runs" / "s001").exists()


class _Stop(Exception):
    pass


def test_campaign_passes_its_data_dir_to_the_scenario_design(tmp_path, monkeypatch):
    """With no design file, the campaign must build the design from *its* data dir's
    breach_params.json -- not from SIH26_DATA_DIR or the repo default."""
    from backend import campaign
    from backend.m0_api import registry
    from backend.m5_emulator import scenario_design

    explicit, env_dir, sites_dir = tmp_path / "explicit", tmp_path / "env", tmp_path / "sites"
    monkeypatch.setenv("SIH26_DATA_DIR", str(env_dir))
    registry.init_db()
    raw = copy.deepcopy(yaml.safe_load((Path(__file__).parents[1] / "fixtures/shared/synth.yaml").read_text()))
    sites_dir.mkdir()
    (sites_dir / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    seen = {}

    def capture(cfg, dam_id, settings=None, data_dir=None, **kwargs):
        seen["data_dir"] = data_dir
        raise _Stop

    monkeypatch.setattr(scenario_design, "build_scenario_design", capture)
    with pytest.raises(_Stop):
        campaign.run_dflowfm_campaign("synth", registry.connect(), explicit, sites_dir)
    assert Path(seen["data_dir"]) == explicit


def test_campaign_default_data_dir_is_sih26_data_dir(tmp_path, monkeypatch):
    from backend import campaign
    from backend.m0_api import registry
    from backend.m5_emulator import scenario_design

    env_dir, sites_dir = tmp_path / "env", tmp_path / "sites"
    monkeypatch.setenv("SIH26_DATA_DIR", str(env_dir))
    registry.init_db()
    raw = yaml.safe_load((Path(__file__).parents[1] / "fixtures/shared/synth.yaml").read_text())
    sites_dir.mkdir()
    (sites_dir / "synth.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    seen = {}

    def capture(cfg, dam_id, settings=None, data_dir=None, **kwargs):
        seen["data_dir"] = data_dir
        raise _Stop

    monkeypatch.setattr(scenario_design, "build_scenario_design", capture)
    with pytest.raises(_Stop):
        campaign.run_dflowfm_campaign("synth", registry.connect(), None, sites_dir)
    assert Path(seen["data_dir"]) == env_dir

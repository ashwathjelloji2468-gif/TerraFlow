"""Tests for backend.shared.site_config — site config model and loader."""

import copy
import json
import warnings
from pathlib import Path

import jsonschema
import pydantic
import pytest
import yaml

from backend.shared.site_config import (
    PlaceholderWarning,
    SiteConfig,
    SiteConfigError,
    load_site_config,
)

from .conftest import SITES_DIR, SYNTH_PLACEHOLDERS, fully_sourced

CASCADE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "m2_breach" / "synth_cascade.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _contracts_schema_store() -> dict:
    schemas_dir = REPO_ROOT / "contracts" / "schemas"
    return {p.name: json.loads(p.read_text()) for p in schemas_dir.glob("*.schema.json")}


@pytest.fixture
def cascade_raw() -> dict:
    with open(CASCADE_FIXTURE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_quiet(path, **kwargs) -> SiteConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        return load_site_config(path, **kwargs)


# --------------------------------------------------------------------------- valid config


def test_synthetic_config_loads(synth_path):
    cfg = load_quiet(synth_path)
    assert isinstance(cfg, SiteConfig)
    assert cfg.site.id == "synth"
    assert cfg.crs.utm_epsg.value == 32645
    assert cfg.domains.far_field.bbox.value == [88.45, 27.45, 88.55, 27.55]
    assert cfg.domains.far_field.grid_resolution.value == 30
    assert cfg.domains.near_field.inflow.from_ == "far_field"
    assert cfg.dams[0].id == "synth_lake"
    assert cfg.dams[0].breach_inputs.dam_type.value == "HD"
    assert cfg.events[0].breach_times["synth_lake"].status == "sourced"


def test_placeholder_fields_listed_exactly(synth_config):
    assert synth_config.placeholder_fields == SYNTH_PLACEHOLDERS
    assert synth_config.has_placeholders is True


def test_load_by_site_id(synth_path):
    cfg = load_quiet("synth", sites_dir=synth_path.parent)
    assert cfg.site.id == "synth"


def test_fully_sourced_config_has_no_placeholders(synth_raw, write_site):
    path = write_site(fully_sourced(synth_raw))
    with warnings.catch_warnings():
        warnings.simplefilter("error", PlaceholderWarning)  # any placeholder warning fails the test
        cfg = load_site_config(path)
    assert cfg.placeholder_fields == []
    assert cfg.has_placeholders is False


# --------------------------------------------------------------------------- placeholder warning


def test_placeholder_warning_is_loud(synth_path):
    with pytest.warns(PlaceholderWarning) as record:
        load_site_config(synth_path)
    assert len(record) == 1, "exactly one consolidated warning per load"
    msg = str(record[0].message)
    # The warning is a single line (the full per-field list lives in the log banner only,
    # so it isn't printed twice) — but it still says how many and points at the log.
    assert msg == f"{synth_path.name}: 4 placeholders (has_placeholders=true; see log for the field list)"


def test_placeholder_warning_also_logged(synth_path, caplog):
    with pytest.warns(PlaceholderWarning), caplog.at_level("WARNING"):
        load_site_config(synth_path)
    assert any("PLACEHOLDER" in r.getMessage() for r in caplog.records)
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "has_placeholders" in log_text
    for field in SYNTH_PLACEHOLDERS:
        assert field in log_text


# --------------------------------------------------------------------------- SourcedValue rules


@pytest.mark.parametrize("missing", ["unit", "source", "status"])
def test_value_missing_required_key_rejected(synth_raw, write_site, missing):
    del synth_raw["dams"][0]["breach_inputs"]["dam_height"][missing]
    with pytest.raises(SiteConfigError, match=missing):
        load_quiet(write_site(synth_raw))


@pytest.mark.parametrize("missing", ["unit", "source", "status"])
def test_nested_value_missing_key_rejected(synth_raw, write_site, missing):
    del synth_raw["events"][0]["breach_times"]["synth_lake"][missing]
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_unknown_status_rejected(synth_raw, write_site):
    synth_raw["crs"]["utm_epsg"]["status"] = "guessed"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_null_value_requires_placeholder(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["dam_height"]["value"] = None
    with pytest.raises(SiteConfigError, match="null"):
        load_quiet(write_site(synth_raw))


def test_sourced_value_requires_nonempty_source(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["dam_height"]["source"] = "  "
    with pytest.raises(SiteConfigError, match="source"):
        load_quiet(write_site(synth_raw))


def test_placeholder_may_have_empty_source(synth_raw, write_site):
    synth_raw["points_of_interest"][1]["location"]["source"] = ""
    cfg = load_quiet(write_site(synth_raw))
    assert "points_of_interest[1].location" in cfg.placeholder_fields


def test_unknown_unit_rejected(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["dam_height"]["unit"] = "ft"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_wrong_unit_for_field_rejected(synth_raw, write_site):
    synth_raw["domains"]["far_field"]["bbox"]["unit"] = "m"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_extra_key_rejected(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["dam_height"]["units"] = "m"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_bad_enum_value_rejected(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["failure_mode"]["value"] = "overtopping"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_bad_bbox_order_rejected(synth_raw, write_site):
    synth_raw["domains"]["far_field"]["bbox"]["value"] = [88.55, 27.45, 88.45, 27.55]
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_point_out_of_range_rejected(synth_raw, write_site):
    synth_raw["points_of_interest"][0]["location"]["value"] = [27.49, 188.49]
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_non_utm_epsg_rejected(synth_raw, write_site):
    synth_raw["crs"]["utm_epsg"]["value"] = 4326
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_bad_datetime_rejected(synth_raw, write_site):
    synth_raw["events"][0]["onset"]["value"] = "3rd October"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


# --------------------------------------------------------------------------- initial_water_level, volume_elevation


def test_initial_water_level_absent_by_default(synth_raw, write_site):
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.dams[0].initial_water_level is None


def test_initial_water_level_loads_and_lists_as_placeholder(synth_raw, write_site):
    synth_raw["dams"][0]["initial_water_level"] = {
        "value": None, "unit": "m", "source": "", "status": "placeholder",
    }
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.dams[0].initial_water_level.status == "placeholder"
    assert "dams[0].initial_water_level" in cfg.placeholder_fields


def test_initial_water_level_wrong_unit_rejected(synth_raw, write_site):
    synth_raw["dams"][0]["initial_water_level"] = {
        "value": None, "unit": "m^3", "source": "", "status": "placeholder",
    }
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_base_flow_absent_by_default(synth_raw, write_site):
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.domains.far_field.inflow.base_flow is None


def test_base_flow_loads_and_lists_as_placeholder(synth_raw, write_site):
    synth_raw["domains"]["far_field"]["inflow"]["base_flow"] = {
        "value": None, "unit": "m^3/s", "source": "", "status": "placeholder",
    }
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.domains.far_field.inflow.base_flow.status == "placeholder"
    assert "domains.far_field.inflow.base_flow" in cfg.placeholder_fields


def test_base_flow_wrong_unit_rejected(synth_raw, write_site):
    synth_raw["domains"]["far_field"]["inflow"]["base_flow"] = {
        "value": 5.0, "unit": "m^3", "source": "", "status": "sourced",
    }
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_simulation_sph_scenarios_empty_by_default(synth_raw, write_site):
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.simulation.sph.scenarios == []


def test_simulation_sph_scenarios_loads(synth_raw, write_site):
    synth_raw["simulation"] = {"sph": {"scenarios": ["synth__s001", "synth__demo_s001",
                                                       "synth__hist_synth_event", "synth__n_full_volume"]}}
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.simulation.sph.scenarios == [
        "synth__s001", "synth__demo_s001", "synth__hist_synth_event", "synth__n_full_volume",
    ]


def test_simulation_sph_scenario_wrong_site_prefix_rejected(synth_raw, write_site):
    synth_raw["simulation"] = {"sph": {"scenarios": ["other_site__s001"]}}
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_simulation_sph_scenario_bad_shape_rejected(synth_raw, write_site):
    synth_raw["simulation"] = {"sph": {"scenarios": ["synth__not_a_real_shape"]}}
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw))


def test_volume_elevation_area_relation_placeholder_lists(synth_raw, write_site):
    synth_raw["dams"][0]["volume_elevation"] = {
        "method": "area_volume_relation",
        "area_volume_exponent_b": {"value": None, "unit": "-", "source": "", "status": "placeholder"},
    }
    cfg = load_quiet(write_site(synth_raw))
    assert cfg.dams[0].volume_elevation.method == "area_volume_relation"
    assert "dams[0].volume_elevation.area_volume_exponent_b" in cfg.placeholder_fields


def test_volume_elevation_area_relation_needs_exponent(synth_raw, write_site):
    synth_raw["dams"][0]["volume_elevation"] = {"method": "area_volume_relation"}
    with pytest.raises(SiteConfigError, match="area_volume_exponent_b"):
        load_quiet(write_site(synth_raw))


# --------------------------------------------------------------------------- cross-field rules


def test_site_id_must_match_file_name(synth_raw, write_site):
    with pytest.raises(SiteConfigError, match="file name"):
        load_quiet(write_site(synth_raw, stem="other_name"))


def test_bad_site_id_pattern_rejected(synth_raw, write_site):
    synth_raw["site"]["id"] = "Synth-Site"
    with pytest.raises(SiteConfigError):
        load_quiet(write_site(synth_raw, stem="Synth-Site"))


def test_unknown_triggered_by_rejected(synth_raw, write_site):
    synth_raw["dams"][0]["triggered_by"] = "no_such_dam"
    with pytest.raises(SiteConfigError, match="triggered_by"):
        load_quiet(write_site(synth_raw))


def test_unknown_inflow_from_rejected(synth_raw, write_site):
    synth_raw["domains"]["far_field"]["inflow"]["from"] = "no_such_dam"
    with pytest.raises(SiteConfigError, match="inflow"):
        load_quiet(write_site(synth_raw))


def test_unknown_breach_time_dam_rejected(synth_raw, write_site):
    bt = synth_raw["events"][0]["breach_times"]
    bt["ghost_dam"] = bt.pop("synth_lake")
    with pytest.raises(SiteConfigError, match="breach_times"):
        load_quiet(write_site(synth_raw))


def test_duplicate_dam_ids_rejected(synth_raw, write_site):
    import copy

    synth_raw["dams"].append(copy.deepcopy(synth_raw["dams"][0]))
    with pytest.raises(SiteConfigError, match="duplicate"):
        load_quiet(write_site(synth_raw))


def test_near_field_outside_far_field_rejected(synth_raw, write_site):
    synth_raw["domains"]["near_field"]["bbox"]["value"] = [88.54, 27.48, 88.60, 27.50]
    with pytest.raises(SiteConfigError, match="near_field"):
        load_quiet(write_site(synth_raw))


def test_non_dividing_cell_sizes_rejected(synth_raw, write_site):
    synth_raw["domains"]["near_field"]["grid_resolution"]["value"] = 7
    with pytest.raises(SiteConfigError, match="divide"):
        load_quiet(write_site(synth_raw))


def test_breach_higher_than_dam_warns(synth_raw, write_site):
    synth_raw["dams"][0]["breach_inputs"]["breach_height"]["value"] = 40
    with pytest.warns(UserWarning, match="breach_height"):
        load_quiet(write_site(synth_raw))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_site_config(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- cascade (M2 two_stage_imposed)


def test_cascade_fixture_loads(cascade_raw):
    cfg = SiteConfig.model_validate(cascade_raw)
    assert cfg.cascade.approach == "two_stage_imposed"
    assert cfg.dams[1].trigger.value.value == 500.0
    assert cfg.dams[1].imposed_ranges.peak_discharge_m3s.value == [800.0, 1500.0]


def test_concrete_dam_must_set_equations_applicable_false(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    raw["dams"][1]["equations_applicable"] = True
    with pytest.raises(pydantic.ValidationError, match="equations_applicable"):
        SiteConfig.model_validate(raw)


def test_equations_applicable_false_requires_imposed_ranges(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    del raw["dams"][1]["imposed_ranges"]
    with pytest.raises(pydantic.ValidationError, match="imposed_ranges"):
        SiteConfig.model_validate(raw)


def test_trigger_on_untriggered_dam_rejected(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    raw["dams"][0]["trigger"] = copy.deepcopy(raw["dams"][1]["trigger"])
    with pytest.raises(pydantic.ValidationError, match="trigger"):
        SiteConfig.model_validate(raw)


def test_triggered_by_without_cascade_block_rejected(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    del raw["cascade"]
    with pytest.raises(pydantic.ValidationError, match="cascade"):
        SiteConfig.model_validate(raw)


def test_two_stage_imposed_requires_trigger_on_triggered_dam(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    del raw["dams"][1]["trigger"]
    with pytest.raises(pydantic.ValidationError, match="trigger"):
        SiteConfig.model_validate(raw)


def test_imposed_range_low_above_high_rejected(cascade_raw):
    raw = copy.deepcopy(cascade_raw)
    raw["dams"][1]["imposed_ranges"]["peak_discharge_m3s"]["value"] = [1500.0, 800.0]
    with pytest.raises(pydantic.ValidationError, match="low <= high"):
        SiteConfig.model_validate(raw)


def test_dambreak_structure_cascade_approach_loads(cascade_raw):
    """The loader accepts 'dambreak_structure' (it's M2's cascade.py that refuses to act on
    it — see tests/m2_breach/test_cascade.py)."""
    raw = copy.deepcopy(cascade_raw)
    raw["cascade"]["approach"] = "dambreak_structure"
    cfg = SiteConfig.model_validate(raw)
    assert cfg.cascade.approach == "dambreak_structure"


# --------------------------------------------------------------------------- real config smoke test


def test_teesta_config_loads_with_placeholder_warning():
    with pytest.warns(PlaceholderWarning):
        cfg = load_site_config(SITES_DIR / "teesta.yaml")
    assert cfg.site.id == "teesta"
    assert cfg.crs.utm_epsg.value == 32645
    assert cfg.crs.utm_epsg.status == "sourced"
    assert cfg.has_placeholders is True
    assert [d.id for d in cfg.dams] == ["south_lhonak", "teesta_iii"]
    for dam in cfg.dams:
        assert dam.equations_applicable is True
        assert dam.volume_elevation is not None
        assert dam.volume_elevation.method == "area_volume_relation"
    assert cfg.dams[1].initial_water_level is not None
    assert cfg.dams[1].initial_water_level.status == "placeholder"

    schema = json.loads((REPO_ROOT / "contracts" / "schemas" / "site_config.schema.json").read_text())
    resolver = jsonschema.RefResolver(base_uri="", referrer=schema, store=_contracts_schema_store())
    validator = jsonschema.Draft202012Validator(schema, resolver=resolver)
    raw = yaml.safe_load((SITES_DIR / "teesta.yaml").read_text())
    errors = list(validator.iter_errors(raw))
    assert errors == []


# --- Feature 1: default lookup also finds onboarded sites ----------------------------------------
def test_default_lookup_finds_an_onboarded_site_config(synth_raw, tmp_path, monkeypatch):
    from backend.shared.site_config import load_site_config

    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "data"))
    synth_raw["site"]["id"] = "onboarded_x"
    path = tmp_path / "data" / "onboarded_x" / "config" / "onboarded_x.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(synth_raw))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert load_site_config("onboarded_x").site.id == "onboarded_x"


def test_bundled_repo_config_wins_over_a_data_copy(tmp_path, monkeypatch):
    from backend.shared.site_config import SITES_DIR, _resolve_path

    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "data"))
    shadow = tmp_path / "data" / "teesta" / "config" / "teesta.yaml"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("site: {id: teesta}\n")
    assert _resolve_path("teesta", None) == SITES_DIR / "teesta.yaml"


def test_explicit_sites_dir_is_unchanged(tmp_path):
    from backend.shared.site_config import _resolve_path

    assert _resolve_path("anything", tmp_path) == tmp_path / "anything.yaml"

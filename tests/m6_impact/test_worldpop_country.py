"""Feature 16: optional site.country_iso3; WorldPop never defaults to India and never infers a country."""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from backend.m6_impact import exposure_worldpop as wp
from backend.shared.site_config import PlaceholderWarning, SiteConfig, SiteConfigError, load_site_config

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "contracts" / "schemas" / "site_config.schema.json").read_text())
ALPHA = REPO / "tests" / "fixtures" / "f16" / "synth_river_alpha.yaml"


def _raw(country=None, drop=False) -> dict:
    raw = yaml.safe_load(ALPHA.read_text(encoding="utf-8"))
    if not drop:
        raw["site"]["country_iso3"] = country
    return raw


# --- site config -----------------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["IND", "NPL", "BTN", "ZZZ"])
def test_valid_country_code(code):
    assert SiteConfig.model_validate(_raw(code)).site.country_iso3 == code
    jsonschema.Draft202012Validator(SCHEMA).validate(_raw(code))


def test_missing_country_is_null():
    assert SiteConfig.model_validate(_raw(drop=True)).site.country_iso3 is None
    assert SiteConfig.model_validate(_raw(None)).site.country_iso3 is None


@pytest.mark.parametrize("bad", ["IN", "ind", "India", "IND1", "I N", "", 356, "IN-D"])
def test_invalid_country_rejected(bad, tmp_path):
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(_raw(bad))
    assert list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(_raw(bad)))
    raw = _raw(bad)
    (tmp_path / "synth_river_alpha.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(SiteConfigError, match="country_iso3"):
        load_site_config("synth_river_alpha", sites_dir=tmp_path)


def test_schema_file_is_regenerated_from_the_model():
    from backend.shared.site_config import SiteConfig as M
    assert SCHEMA == {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "site_config.schema.json",
                      **M.model_json_schema()}
    assert "country_iso3" in SCHEMA["$defs"]["Site"]["properties"]
    assert "country_iso3" not in SCHEMA["$defs"]["Site"].get("required", [])


def test_existing_configs_still_validate_without_country():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        teesta = load_site_config("teesta")
    assert teesta.site.country_iso3 is None  # sites/teesta.yaml unchanged: country not stated
    for name in ("teesta", "demo_valley", "synth_engdam"):
        raw = yaml.safe_load((REPO / "sites" / f"{name}.yaml").read_text(encoding="utf-8"))
        assert list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(raw)) == [], name


# --- WorldPop country resolution ---------------------------------------------------------------------
def test_no_default_country_symbol():
    assert not hasattr(wp, "DEFAULT_ISO3")
    import inspect
    for fn in (wp.worldpop_url, wp.download_national_raster, wp.fetch):
        p = inspect.signature(fn).parameters["iso3"]
        assert p.default in (inspect.Parameter.empty, None), fn.__name__


def test_missing_country_requires_explicit_iso3():
    cfg = SiteConfig.model_validate(_raw(drop=True))
    with pytest.raises(wp.CountryUnknown, match="pass --iso3"):
        wp.resolve_iso3(cfg)
    assert wp.resolve_iso3(cfg, "IND") == ("IND", "explicit --iso3")


def test_configured_country_used():
    assert wp.resolve_iso3(SiteConfig.model_validate(_raw("NPL"))) == ("NPL", "site.country_iso3")
    assert wp.resolve_iso3(SiteConfig.model_validate(_raw("NPL")), "NPL") == ("NPL", "explicit --iso3")


def test_conflicting_or_invalid_explicit_iso3_refused():
    with pytest.raises(ValueError, match="conflicts"):
        wp.resolve_iso3(SiteConfig.model_validate(_raw("NPL")), "IND")
    with pytest.raises(ValueError, match="3-letter"):
        wp.resolve_iso3(SiteConfig.model_validate(_raw(drop=True)), "ind")


def test_region_and_bbox_never_used_to_infer_country():
    raw = _raw(drop=True)
    raw["site"]["region"] = "India; Sikkim"  # text that would 'suggest' a country
    with pytest.raises(wp.CountryUnknown):
        wp.resolve_iso3(SiteConfig.model_validate(raw))


def test_fetch_without_country_refuses_before_any_download(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(wp, "download_national_raster", lambda *a, **k: calls.append(a) or None)
    cfg = SiteConfig.model_validate(_raw(drop=True))
    with pytest.raises(wp.CountryUnknown):
        wp.fetch(cfg, data_dir=tmp_path)
    assert calls == [] and not (tmp_path / cfg.site.id).exists()


def test_non_india_synthetic_site_uses_its_own_country(tmp_path, monkeypatch):
    seen = {}

    def fake_download(cache_dir, iso3, year=wp.WORLDPOP_YEAR, client=None):
        seen["iso3"] = iso3
        raise RuntimeError("stop after country resolution")  # no network in tests
    monkeypatch.setattr(wp, "download_national_raster", fake_download)
    with pytest.raises(RuntimeError, match="stop after"):
        wp.fetch(SiteConfig.model_validate(_raw("NPL")), data_dir=tmp_path)
    assert seen == {"iso3": "NPL"}
    assert "npl_ppp_2020" in wp.worldpop_url("NPL") and "/NPL/" in wp.worldpop_url("NPL")


def test_cli_refuses_without_country(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wp, "load_site_config", lambda sid: SiteConfig.model_validate(_raw(drop=True)))
    monkeypatch.setattr(wp, "download_national_raster", lambda *a, **k: pytest.fail("downloaded"))
    assert wp.main(["synth_river_alpha", "--data-dir", str(tmp_path)]) == 2
    assert "pass --iso3" in capsys.readouterr().err


def test_cli_explicit_iso3(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(wp, "load_site_config", lambda sid: SiteConfig.model_validate(_raw(drop=True)))
    monkeypatch.setattr(wp, "fetch", lambda cfg, data_dir, iso3: seen.update(iso3=iso3) or {})
    assert wp.main(["synth_river_alpha", "--iso3", "IND", "--data-dir", str(tmp_path)]) == 0
    assert seen == {"iso3": "IND"}

"""Feature 16 P1: credential-like keys can never enter a site config (the strict model already
forbids every unknown key, at every level), and a rejected credential VALUE is never echoed into
the loader's error message. No real secrets: the values below are inert test strings."""
from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from backend.shared.site_config import SiteConfig, SiteConfigError, load_site_config

SECRET = "TEST-NOT-A-REAL-SECRET-7f3a9c"
KEYS = ["api_key", "password", "token", "secret", "private_key", "service_account_key",
        "gee_service_account_key", "OPENTOPOGRAPHY_API_KEY", "credentials", "auth"]

# (description, function placing {key: SECRET} somewhere in the raw config)
LOCATIONS = {
    "root": lambda c, k: c.__setitem__(k, SECRET),
    "site": lambda c, k: c["site"].__setitem__(k, SECRET),
    "crs": lambda c, k: c["crs"].__setitem__(k, SECRET),
    "sourced_value": lambda c, k: c["crs"]["utm_epsg"].__setitem__(k, SECRET),
    "domain": lambda c, k: c["domains"]["far_field"].__setitem__(k, SECRET),
    "dam": lambda c, k: c["dams"][0].__setitem__(k, SECRET),
    "breach_inputs": lambda c, k: c["dams"][0]["breach_inputs"].__setitem__(k, SECRET),
    "poi": lambda c, k: c["points_of_interest"][0].__setitem__(k, SECRET),
    "event": lambda c, k: c["events"][0].__setitem__(k, SECRET),
}


@pytest.fixture
def raw(synth_raw):
    assert SiteConfig.model_validate(copy.deepcopy(synth_raw))  # baseline is valid
    return synth_raw


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("where", sorted(LOCATIONS))
def test_credential_like_key_rejected_everywhere(raw, where, key):
    LOCATIONS[where](raw, key)
    with pytest.raises(ValidationError) as exc:
        SiteConfig.model_validate(raw)
    assert any(e["type"] == "extra_forbidden" and e["loc"][-1] == key for e in exc.value.errors())


def test_every_model_forbids_extra_keys():
    """Guard against weakening: every sub-model of SiteConfig keeps extra='forbid'."""
    from pydantic import BaseModel
    seen, stack = set(), [SiteConfig]
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        assert m.model_config.get("extra") == "forbid", m.__name__
        for f in m.model_fields.values():
            for t in _models_in(f.annotation):
                stack.append(t)
    assert len(seen) > 10


def _models_in(tp):
    from typing import get_args

    from pydantic import BaseModel
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        yield tp
    for a in get_args(tp) or ():
        yield from _models_in(a)


@pytest.mark.parametrize("where", ["root", "sourced_value", "dam"])
def test_loader_error_never_echoes_the_secret(raw, write_site, where):
    LOCATIONS[where](raw, "api_key")
    path = write_site(raw)
    with pytest.raises(SiteConfigError) as exc:
        load_site_config(path.stem, sites_dir=path.parent)
    text = str(exc.value)
    assert "api_key" in text and "Extra inputs are not permitted" in text
    assert SECRET not in text and "input_value" not in text
    assert exc.value.__cause__ is None  # no chained ValidationError carrying the value


def test_loader_error_hides_value_on_type_errors_too(raw, write_site):
    raw["crs"]["utm_epsg"]["value"] = SECRET  # wrong type: the bad value itself must not be echoed
    path = write_site(raw)
    with pytest.raises(SiteConfigError) as exc:
        load_site_config(path.stem, sites_dir=path.parent)
    assert SECRET not in str(exc.value) and "crs.utm_epsg.value" in str(exc.value)


def test_template_and_bundled_configs_have_no_credential_like_keys():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    bad = {k.lower() for k in KEYS}

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield str(k).lower()
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)
    for path in sorted((repo / "sites").glob("*.yaml")):
        assert not bad & set(keys(yaml.safe_load(path.read_text(encoding="utf-8")))), path.name

"""Tests for backend.campaign (CLAUDE.md architecture table: "Runs whole
scenario sets through M3/M4"). This session only builds the SPH (M4) path
(backend/campaign.py's own docstring explains why M3/launching are out of
scope).

`backend.m4_sph.generator` needs geopandas, which isn't installed in every
dev environment this repo is tested in; these tests stub it out via
`sys.modules` (a local import inside `run_sph_campaign` looks it up there
before trying to import the real module), so campaign.py's own queueing/
VRAM-refusal/job-bookkeeping logic is testable independently of M4's real
case-building machinery.

`_install_fake_generator` must patch *both* `sys.modules["backend.m4_sph.
generator"]` and `backend.m4_sph`'s own `generator` attribute: campaign.py's
`from backend.m4_sph import generator` resolves via `getattr(backend.m4_sph,
"generator")` once that attribute exists (e.g. because some other, unrelated
test in the same pytest process did `import backend.m4_sph.generator` for
real first, which binds it as an attribute of the `backend.m4_sph` package
object) -- at that point the import machinery never consults `sys.modules`
again, and patching only `sys.modules` silently does nothing.
"""

from __future__ import annotations

import copy
import json
import sys
import types
import warnings
from pathlib import Path

import pytest
import yaml

import backend.m4_sph
from backend.campaign import run_sph_campaign
from backend.m0_api import jobs, registry

SYNTH_PATH = Path(__file__).resolve().parent / "fixtures" / "shared" / "synth.yaml"


def _synth_raw_with_sph_scenarios(scenario_ids: list[str]) -> dict:
    with open(SYNTH_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = copy.deepcopy(data)
    data["domains"]["near_field"]["inflow"]["location"] = {
        "value": [88.485, 27.499], "unit": "deg", "source": "test", "status": "sourced",
    }
    data["dams"][0]["breach_inputs"]["water_volume_above_invert"] = {
        "value": 1_000_000, "unit": "m^3", "source": "test", "status": "sourced",
    }
    data["points_of_interest"][1]["location"] = {
        "value": [88.52, 27.47], "unit": "deg", "source": "test", "status": "sourced",
    }
    data["events"][0]["imagery_post_event"] = {
        "value": "2020-01-05", "unit": "iso8601", "source": "test", "status": "sourced",
    }
    data["simulation"] = {"sph": {"scenarios": scenario_ids}}
    return data


def _write_site(raw: dict, sites_dir: Path) -> None:
    sites_dir.mkdir(parents=True, exist_ok=True)
    with open(sites_dir / f"{raw['site']['id']}.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)


def _write_design(data_dir: Path, site_id: str, scenario_ids: list[str]) -> None:
    design = {
        "contract_version": "0.3.0", "site_id": site_id, "model": "delft3d",
        "method": "maximin_lhs", "seed": 42, "n": len(scenario_ids),
        "inputs": [], "extra": [],
        "scenarios": [
            {"scenario_id": sid, "kind": "design",
             "params": {"water_volume_m3": 1_000_000.0, "breach_width_m": 60.0, "failure_time_s": 1800.0}}
            for sid in scenario_ids
        ],
        "has_placeholders": True, "caveats": [],
    }
    out = data_dir / site_id / "design"
    out.mkdir(parents=True, exist_ok=True)
    (out / "scenario_design.json").write_text(json.dumps(design), encoding="utf-8")


class _FakeOverVramBudget(Exception):
    pass


class _FakeInflowUnavailable(Exception):
    pass


def _install_fake_generator(monkeypatch, over_budget_scenarios: set[str] = frozenset()):
    fake = types.ModuleType("backend.m4_sph.generator")
    fake.OverVramBudget = _FakeOverVramBudget
    fake.InflowUnavailable = _FakeInflowUnavailable

    def build_nearfield_case(site_id, scenario_id, params, data_dir=None, sites_dir=None, production=False):
        if scenario_id in over_budget_scenarios:
            raise _FakeOverVramBudget(f"{scenario_id}: predicted VRAM exceeds budget")
        spec = object()
        case_meta = {"scenario_id": scenario_id, "site_id": site_id, "model": "sph", "dp_m": 0.05}
        return spec, case_meta

    def write_case(spec, case_meta, run_dir, terrain_dir):
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        return Path(run_dir) / "case"

    fake.build_nearfield_case = build_nearfield_case
    fake.write_case = write_case
    monkeypatch.setitem(sys.modules, "backend.m4_sph.generator", fake)
    # Also patch the backend.m4_sph package's own `generator` attribute (see the module
    # docstring): restores whatever it was before (unset, or the real module) on teardown.
    monkeypatch.setattr(backend.m4_sph, "generator", fake, raising=False)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "data"))
    registry.init_db()
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir):
    c = registry.connect()
    yield c
    c.close()


@pytest.fixture
def sites_dir(tmp_path) -> Path:
    return tmp_path / "sites"


def _load_quiet(*args, **kwargs):
    from backend.shared.site_config import load_site_config
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_site_config(*args, **kwargs)


def test_no_sph_scenarios_returns_none_and_no_job(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios([]), sites_dir)

    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    assert job_id is None
    assert results == []
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_scenario_missing_design_file_is_refused(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    # no scenario_design.json written

    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    assert job_id is not None
    assert len(results) == 1
    assert results[0].status == "refused"
    assert "scenario_design.json" in results[0].reason
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_scenario_missing_from_design_is_refused(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001", "synth__s002"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])  # s002 missing

    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    by_id = {r.scenario_id: r for r in results}
    assert by_id["synth__s001"].status == "queued"
    assert by_id["synth__s002"].status == "refused"
    assert "not found in design" in by_id["synth__s002"].reason


def test_over_vram_budget_case_is_refused_not_queued(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch, over_budget_scenarios={"synth__s002"})
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001", "synth__s002"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001", "synth__s002"])

    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    by_id = {r.scenario_id: r for r in results}
    assert by_id["synth__s001"].status == "queued"
    assert by_id["synth__s002"].status == "refused"
    assert "VRAM" in by_id["synth__s002"].reason

    run_ids = {row[0] for row in conn.execute("SELECT run_id FROM runs").fetchall()}
    assert run_ids == {"synth__s001__sph"}


def test_queued_case_writes_runs_row_and_case_dir(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])

    job_id, results = run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    assert results[0].run_id == "synth__s001__sph"
    row = conn.execute("SELECT scenario_id, model, status, run_dir, meta_json FROM runs "
                       "WHERE run_id = ?", (results[0].run_id,)).fetchone()
    assert row[0] == "synth__s001"
    assert row[1] == "sph"
    assert row[2] == "queued"
    assert (Path(row[3]).parent / "case").exists() or Path(row[3]).exists()
    assert json.loads(row[4])["dp_m"] == 0.05

    job_row = jobs.get_job(conn, job_id)
    assert job_row["kind"] == "campaign"
    assert job_row["site_id"] == "synth"
    payload = jobs.payload(job_row)
    assert payload["model"] == "sph"
    assert payload["scenario_ids"] == ["synth__s001"]
    assert any("queued synth__s001__sph" in e for e in payload["events"])


def test_rerunning_campaign_replaces_existing_runs_row(conn, data_dir, sites_dir, monkeypatch):
    _install_fake_generator(monkeypatch)
    _write_site(_synth_raw_with_sph_scenarios(["synth__s001"]), sites_dir)
    _write_design(data_dir, "synth", ["synth__s001"])

    run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)
    run_sph_campaign("synth", conn, data_dir=data_dir, sites_dir=sites_dir)

    count = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id = ?",
                          ("synth__s001__sph",)).fetchone()[0]
    assert count == 1

"""Feature 1: dynamic site onboarding -- creation, persistence, retrieval, validation, duplicates,
pipeline entry and Teesta backward compatibility (docs/decisions.md 2026-09-30)."""

from __future__ import annotations

import importlib
import json
import re
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.m0_api import jobs, registry, schemas, site_registry
from backend.m0_api.main import app
from backend.m0_api.worker import Worker
from tests.m0_api.conftest import wait_until

REPO = Path(__file__).resolve().parents[2]
API = "/api/v1"
client = TestClient(app)


def minimal_config(site_id: str = "kosi_barrage", name: str = "Kosi Barrage study reach") -> dict:
    """The shape the frontend wizard sends: `sites/template.yaml` with ids/enums filled and the
    far-field bbox given; every other fact stays an honest null placeholder."""
    cfg = yaml.safe_load((REPO / "sites" / "template.yaml").read_text())
    cfg["site"].update({"id": site_id, "name": name})
    cfg["domains"]["far_field"]["bbox"] = {"value": [86.9, 26.4, 87.2, 26.8], "unit": "deg",
                                           "source": "operator entry (onboarding wizard), unverified",
                                           "status": "placeholder"}
    cfg["domains"]["far_field"]["inflow"]["from"] = "main_dam"
    dam = cfg["dams"][0]
    dam.update({"id": "main_dam", "name": "Kosi Barrage", "kind": "embankment_dam"})
    dam["location"] = {"value": [87.0, 26.5], "unit": "deg", "source": "operator entry", "status": "placeholder"}
    cfg["points_of_interest"] = [{**cfg["points_of_interest"][0], "id": "town_a", "name": "Town A",
                                  "category": "village",
                                  "location": {"value": [87.05, 26.45], "unit": "deg", "source": "operator entry",
                                               "status": "placeholder"}}]
    cfg["events"] = []
    return cfg


def post(cfg: dict, demo_mode: bool = False):
    return client.post(f"{API}/sites", json={"site_config": cfg, "demo_mode": demo_mode})


# --- creation, persistence, retrieval ------------------------------------------------------
def test_arbitrary_site_is_created_and_config_persisted_before_the_job(data_dir):
    r = post(minimal_config())
    assert r.status_code == 202, r.text
    schemas.validate("site_create_accepted.schema.json", r.json())
    assert r.json()["site_id"] == "kosi_barrage"
    saved = data_dir / "kosi_barrage" / "config" / "kosi_barrage.yaml"
    assert saved.is_file()  # persisted by the API, not only when the worker starts
    assert yaml.safe_load(saved.read_text()) == minimal_config()
    conn = registry.connect()
    try:
        assert jobs.job_status(conn, r.json()["job_id"])["stage"] == "queued"
    finally:
        conn.close()


def test_created_site_is_listed_and_detail_is_the_saved_config():
    job_id = post(minimal_config()).json()["job_id"]
    listing = client.get(f"{API}/sites").json()
    schemas.validate("site_list.schema.json", listing)
    entry = next(s for s in listing if s["site_id"] == "kosi_barrage")
    assert entry["name"] == "Kosi Barrage study reach"
    assert entry["status"] == "onboarding" and entry["status_reason_key"] == "job_stage_queued"
    assert entry["bbox_lonlat"] == [86.9, 26.4, 87.2, 26.8]
    assert entry["has_placeholders"] is True and entry["emulator_ready"] is False

    detail = client.get(f"{API}/sites/kosi_barrage")
    assert detail.status_code == 200
    body = detail.json()
    schemas.validate("site_detail.schema.json", body)
    assert body["dams"][0]["dam_id"] == "kosi_barrage__main_dam"
    assert body["dams"][0]["key_specs"]["location"]["value"] == [87.0, 26.5]
    assert "water_volume_above_invert" in body["dams"][0]["key_specs"]
    assert body["pois"]["features"][0]["properties"]["poi_id"] == "kosi_barrage__poi__town_a"
    assert body["domain"]["features"][0]["geometry"]["coordinates"][0][0] == [86.9, 26.4]
    assert body["centreline"]["features"] == []  # no M1 output yet: nothing invented
    assert {c["id"] for c in body["caveats"]} == {"placeholder_inputs"}
    assert client.get(f"{API}/jobs/{job_id}").json()["site_id"] == "kosi_barrage"


def test_site_survives_api_restart():
    post(minimal_config())
    import backend.m0_api.main as main_module

    restarted = TestClient(importlib.reload(main_module).app)
    assert restarted.get(f"{API}/sites/kosi_barrage").json()["name"] == "Kosi Barrage study reach"
    assert "kosi_barrage" in {s["site_id"] for s in restarted.get(f"{API}/sites").json()}


def test_onboarded_site_is_visible_to_default_config_lookup():
    """Modules calling `load_site_config(site_id)` (M7, validation) find an onboarded site."""
    from backend.shared.site_config import load_site_config

    post(minimal_config())
    with pytest.warns(Warning):
        assert load_site_config("kosi_barrage").site.name == "Kosi Barrage study reach"


# --- invalid input ------------------------------------------------------------------------
def _mutate(fn):
    cfg = minimal_config()
    fn(cfg)
    return cfg


@pytest.mark.parametrize("mutation, field", [
    (lambda c: c["domains"]["near_field"].update(
        bbox={"value": [80.0, 20.0, 80.1, 20.1], "unit": "deg", "source": "x", "status": "placeholder"}),
     "near_field"),
    (lambda c: c["domains"]["far_field"]["inflow"].update({"from": "no_such_dam"}), "no_such_dam"),
    (lambda c: c["crs"].update(utm_epsg={"value": 32645, "unit": "epsg", "source": "", "status": "sourced"}),
     "crs.utm_epsg"),
    (lambda c: c["site"].update(unexpected_field=1), "unexpected_field"),
    (lambda c: c["domains"]["far_field"].update(
        bbox={"value": None, "unit": "deg", "source": "", "status": "placeholder"}),
     "domains.far_field.bbox.value"),
    (lambda c: c["site"].update(name="   "), "site.name"),
])
def test_invalid_config_is_422_naming_the_field_and_nothing_is_saved(data_dir, mutation, field):
    r = post(_mutate(mutation))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    schemas.validate("error.schema.json", detail)
    assert detail["error"]["code"] in {"invalid_site_config", "invalid_request"}
    assert field in json.dumps(detail)
    assert not (data_dir / "kosi_barrage").exists()
    assert client.get(f"{API}/sites/kosi_barrage").status_code == 404


# --- duplicates ---------------------------------------------------------------------------
def test_duplicate_id_is_409():
    assert post(minimal_config()).status_code == 202
    r = post(minimal_config(name="A different name"))
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "site_already_exists"


@pytest.mark.parametrize("site_id", ["teesta", "template"])
def test_bundled_or_reserved_repo_id_cannot_be_overwritten(site_id):
    r = post(minimal_config(site_id=site_id, name=f"New {site_id}"))
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "site_already_exists"
    assert (REPO / "sites" / f"{site_id}.yaml").is_file()


def test_duplicate_name_is_rejected_case_insensitively():
    assert post(minimal_config()).status_code == 202
    r = post(minimal_config(site_id="kosi_two", name="  KOSI barrage   Study Reach "))
    assert r.status_code == 409
    err = r.json()["detail"]["error"]
    assert err["code"] == "site_name_taken" and err["details"]["existing_site_id"] == "kosi_barrage"


def test_name_of_a_bundled_site_is_taken():
    teesta_name = yaml.safe_load((REPO / "sites" / "teesta.yaml").read_text())["site"]["name"]
    r = post(minimal_config(site_id="teesta_copy", name=teesta_name.upper()))
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "site_name_taken"


def test_concurrent_registration_of_one_id_has_exactly_one_winner():
    codes: list[int] = []
    barrier = threading.Barrier(4)

    def attempt(i: int) -> None:
        barrier.wait()
        codes.append(TestClient(app).post(f"{API}/sites", json={"site_config": minimal_config(name=f"n{i}")}).status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [202, 409, 409, 409]


def test_config_is_rolled_back_if_the_job_cannot_be_queued(data_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(jobs, "create_job", boom)
    with pytest.raises(RuntimeError):
        post(minimal_config())
    assert not (data_dir / "kosi_barrage" / "config" / "kosi_barrage.yaml").exists()


def test_saved_config_that_no_longer_validates_is_explicit(data_dir):
    post(minimal_config())
    path = data_dir / "kosi_barrage" / "config" / "kosi_barrage.yaml"
    path.write_text(path.read_text().replace("embankment_dam", "not_a_kind"))
    r = client.get(f"{API}/sites/kosi_barrage")
    assert r.status_code == 422 and r.json()["detail"]["error"]["code"] == "site_config_invalid"
    assert "kosi_barrage" not in {s["site_id"] for s in client.get(f"{API}/sites").json()}


# --- pipeline entry -----------------------------------------------------------------------
def test_worker_runs_the_job_and_fails_explicitly_when_terrain_inputs_are_incomplete():
    """The wizard-style minimal config leaves the near-field and grid fields empty: the terrain
    stage stops before any download with every missing field listed (Feature 2)."""
    job_id = post(minimal_config()).json()["job_id"]
    worker = Worker()
    worker.acquire_lock()
    worker.recover()
    try:
        wait_until(lambda: client.get(f"{API}/jobs/{job_id}").json()["stage"] == "failed", worker.tick)
    finally:
        worker.close()
    status = client.get(f"{API}/jobs/{job_id}").json()
    schemas.validate("job_status.schema.json", status)
    error = status["error"]["error"]
    assert error["code"] == "terrain_inputs_incomplete"
    assert set(error["details"]["missing_fields"]) == {
        "crs.utm_epsg", "domains.far_field.grid_resolution", "domains.near_field.bbox",
        "domains.near_field.grid_resolution", "dams[0] (main_dam).breach_location"}
    site = client.get(f"{API}/sites/kosi_barrage").json()
    assert site["status"] == "failed" and site["status_reason_key"] == "terrain_inputs_incomplete"


def test_recheck_is_scheduled_for_any_registered_site_with_a_library(data_dir):
    post(minimal_config())
    manifest = data_dir / "kosi_barrage" / "emulator" / "delft3d" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"trained_at": "2026-09-01T00:00:00Z"}))
    conn = registry.connect()
    try:  # the onboarding job is still active, so finish it first
        active = jobs.find_active_job(conn, "kosi_barrage")
        jobs.fail(conn, active, "queued", "test_stop", "stopped by test")
    finally:
        conn.close()
    worker = Worker()
    try:
        assert worker._schedule_rechecks() is True
        rows = worker.conn.execute("SELECT kind FROM jobs WHERE site_id='kosi_barrage' AND kind='recheck'").fetchall()
        assert len(rows) == 1
    finally:
        worker.close()


# --- Teesta backward compatibility ---------------------------------------------------------
def test_teesta_is_listed_as_configured_not_onboarded_with_real_dams():
    entry = next(s for s in client.get(f"{API}/sites").json() if s["site_id"] == "teesta")
    assert entry["status"] == "onboarding" and entry["status_reason_key"] == "configured_not_onboarded"
    detail = client.get(f"{API}/sites/teesta").json()
    schemas.validate("site_detail.schema.json", detail)
    lhonak = next(d for d in detail["dams"] if d["dam_id"].endswith("__south_lhonak"))
    assert {"location", "breach_location"} <= set(lhonak["key_specs"])
    assert client.put(f"{API}/sites/teesta/recheck", json={"frequency_days": 30}).status_code == 200


def test_bundled_list_comes_from_config():
    assert site_registry.bundled_site_ids() == ["teesta"]
    assert "demo_valley" not in site_registry.list_site_ids()  # fixtures are not bundled


def test_no_hard_coded_known_site_ids_left_in_backend():
    offenders = [str(p) for p in (REPO / "backend").rglob("*.py") if re.search(r"KNOWN_SITE_IDS", p.read_text())]
    assert offenders == []


# --- Feature 16 P1: credential-like keys -------------------------------------------------------
_F16_SECRET = "TEST-NOT-A-REAL-SECRET-b81e"


@pytest.mark.parametrize("place", ["root", "site", "bbox"])
def test_post_sites_rejects_credential_like_key_without_echoing_it(data_dir, place):
    cfg = minimal_config("cred_probe")
    target = {"root": cfg, "site": cfg["site"], "bbox": cfg["domains"]["far_field"]["bbox"]}[place]
    target["api_key"] = _F16_SECRET
    r = post(cfg)
    assert r.status_code == 422, r.text
    assert _F16_SECRET not in r.text
    assert not (data_dir / "cred_probe").exists()  # nothing persisted

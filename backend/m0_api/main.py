"""M0 — FastAPI orchestrator (contract §5).

Most endpoints are still mocks. They serve the example JSON in
`contracts/examples/` (contract §8: "The frontend's mock mode serves the
example files from contracts/, so mocks can never drift from the contract").
Every response is validated against its `contracts/schemas/*.json` schema
before it is sent.

Real so far: the job system. `POST /sites` creates an onboarding job in
`data/registry.sqlite` and `GET /jobs/{job_id}` reads it back. The API holds
no job state of its own, so restarting it loses nothing. A separate process,
`python -m backend.m0_api.worker`, does the work: every stage is a fake task
for now (except a `recheck` job's `checking` stage, which is real -- see
`worker.py`), and solver runs are detached fake-solver processes.

Also real: `GET /sites`/`GET /sites/{id}` overlay `backend.m0_api.site_status`
(the worker's scheduled lake-area + library-age re-checks) onto the mocked
base response; `PUT /sites/{id}/recheck` persists the schedule for real.
`POST /sites/{id}/rerun` queues a genuine job, but an `onboarding`-kind one --
`jobs.STAGES` has no `rerun` entry yet (contract §5.3 lists no stages for it),
so this does not actually skip terrain the way "re-run" implies.

Sites are real: `site_registry` serves every registered site (bundled in `config/sites.yaml`, or
onboarded through `POST /sites` and persisted at `data/<site_id>/config/<site_id>.yaml`), and
`GET /sites`/`GET /sites/{id}` are built from the saved config plus the site's real job state.

Still mocked: most other endpoints. Scene3D builds real assets when a query's median depth and its
site terrain are present, otherwise it serves the contract example.

Run: `uvicorn backend.m0_api.main:app --reload --port 8000`
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from backend.m0_api import jobs, mock_files, mocks, onboarding, registry, rendering, schemas, site_registry, site_status
from backend.m0_api import compare as api_compare
from backend.m0_api import real_query
from backend.m0_api import scene3d as api_scene3d
from backend.m0_api import timeline as api_timeline
from backend.m0_api import real_timeline
from backend.m0_api import real_impact
from backend.m0_api import validation_helpers
from backend.m7_gee import cache as gee_cache
from backend.m7_gee import fetch as gee_fetch
from backend.m7_gee import imagery as gee_imagery
from backend.shared.site_config import SiteConfigError, load_site_config

log = logging.getLogger("m0.main")

app = FastAPI(title="SIH26 GLOF/dam-break decision-support API", version="0.3.0")

# Contract §5: "CORS allows the Vite dev server (http://localhost:5173)."
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API = "/api/v1"

# --- ID patterns (contract §1.7) --------------------------------------------
SITE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,31}$"
SiteIdPath = Annotated[str, Path(pattern=SITE_ID_PATTERN)]
QueryIdPath = Annotated[str, Path(pattern=r"^q_\d{8}T\d{6}Z_[0-9a-f]{6}$")]
JobIdPath = Annotated[str, Path(pattern=r"^job_\d{8}T\d{6}Z_[0-9a-f]{6}$")]


# --- helpers -----------------------------------------------------------------
def _validated_json(schema_name: str, payload: Any, status_code: int = 200) -> JSONResponse:
    """Validate `payload` against `schema_name`, or fail loudly: a payload
    this server itself builds should always match its own contract."""
    try:
        schemas.validate(schema_name, payload)
    except schemas.ContractViolation as exc:
        raise HTTPException(
            status_code=500,
            detail=mocks.error("contract_violation", f"Server built a response that violates its own contract: {exc}"),
        ) from exc
    return JSONResponse(content=payload, status_code=status_code)


def _require_known_site(site_id: str) -> None:
    if not site_registry.exists(site_id):
        raise HTTPException(
            status_code=404,
            detail=mocks.error("site_not_found", f"No site '{site_id}' is configured.", {"site_id": site_id}),
        )


def _site_config_or_error(site_id: str):
    """A registered site's saved config, or an explicit 422 if the saved file no longer validates."""
    _require_known_site(site_id)
    try:
        return site_registry.load(site_id)
    except SiteConfigError as exc:
        raise HTTPException(status_code=422, detail=mocks.error(
            "site_config_invalid", f"The saved configuration for site '{site_id}' is invalid: {exc}",
            {"site_id": site_id})) from exc


def _validate_request_body(schema_name: str, body: Any) -> None:
    try:
        schemas.validate(schema_name, body)
    except schemas.ContractViolation as exc:
        raise HTTPException(status_code=422, detail=mocks.error("invalid_request", str(exc))) from exc


# =============================================================================
# 1. GET /health
# =============================================================================
@app.get(f"{API}/health")
def get_health() -> JSONResponse:
    return _validated_json("health.schema.json", mocks.mock_response("health.example.json"))


# =============================================================================
# 2. GET /styles
# =============================================================================
@app.get(f"{API}/styles")
def get_styles() -> JSONResponse:
    import json

    styles = json.loads((schemas.CONTRACTS_DIR / "styles.json").read_text())
    return _validated_json("styles.schema.json", styles)


# =============================================================================
# 3-4. GET /sites, GET /sites/{site_id}
# =============================================================================
@app.get(f"{API}/sites")
def list_sites() -> JSONResponse:
    """Every registered site, built from its saved config and real job state. A site whose saved
    config no longer validates is left out of the list (and logged); `GET /sites/{id}` reports it
    explicitly as `site_config_invalid`."""
    sites = []
    for site_id in site_registry.list_site_ids():
        try:
            sites.append(site_status.overlay(site_id, site_registry.summary(site_id)))
        except (FileNotFoundError, SiteConfigError) as exc:
            log.warning("list_sites: skipping site '%s' with an unreadable config: %s", site_id, exc)
    return _validated_json("site_list.schema.json", sites)


@app.get(f"{API}/sites/{{site_id}}")
def get_site(site_id: SiteIdPath) -> JSONResponse:
    _site_config_or_error(site_id)
    detail = site_status.overlay(site_id, site_registry.detail(site_id))
    return _validated_json("site_detail.schema.json", detail)


# =============================================================================
# 5. POST /sites (Add a Dam)
# =============================================================================
@app.post(f"{API}/sites", status_code=202)
def create_site(body: Annotated[dict, Body(...)]) -> JSONResponse:
    _validate_request_body("site_create_request.schema.json", body)
    site_config = body["site_config"]
    site_id = site_config.get("site", {}).get("id") if isinstance(site_config.get("site"), dict) else None
    if not isinstance(site_id, str) or not re.match(SITE_ID_PATTERN, site_id):
        raise HTTPException(
            status_code=422,
            detail=mocks.error("invalid_request", f"site_config.site.id must match {SITE_ID_PATTERN}.", {"field": "site_config.site.id"}),
        )
    demo_mode = bool(body.get("demo_mode", False))
    try:
        # Persist the validated config first, then queue the job inside the same registration:
        # if queuing fails the config is rolled back, so no half-registered site is left behind.
        with site_registry.register(site_config):
            conn = registry.connect()
            try:
                job_id = jobs.create_job(
                    conn, "onboarding", site_id, demo_mode=demo_mode,
                    payload={"site_config": site_config,
                             "i1_synthetic": site_registry.is_synthetic_fixture(site_id) and demo_mode})
            finally:
                conn.close()
    except site_registry.SiteRegistrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=mocks.error(exc.code, exc.message, exc.details)) from exc
    return _validated_json("site_create_accepted.schema.json", {"job_id": job_id, "site_id": site_id}, status_code=202)


# =============================================================================
# 6. GET /jobs/{job_id}
# =============================================================================
@app.get(f"{API}/jobs/{{job_id}}")
def get_job(job_id: JobIdPath) -> JSONResponse:
    conn = registry.connect()
    try:
        status = jobs.job_status(conn, job_id)
    finally:
        conn.close()
    if status is None:
        raise HTTPException(status_code=404, detail=mocks.error("job_not_found", f"No job '{job_id}'.", {"job_id": job_id}))
    return _validated_json("job_status.schema.json", status)


# =============================================================================
# 7. PUT /sites/{site_id}/recheck
# =============================================================================
@app.put(f"{API}/sites/{{site_id}}/recheck")
def set_recheck(site_id: SiteIdPath, body: Annotated[dict, Body(...)]) -> JSONResponse:
    _require_known_site(site_id)
    _validate_request_body("recheck_request.schema.json", body)
    site_status.set_frequency(
        site_id, body["frequency_days"], body.get("lake_area_change_threshold_pct")
    )
    summary = site_status.overlay(site_id, site_registry.summary(site_id, _site_config_or_error(site_id)))
    return _validated_json("site_summary.schema.json", summary)


# =============================================================================
# 8. POST /sites/{site_id}/rerun
# =============================================================================
@app.post(f"{API}/sites/{{site_id}}/rerun", status_code=202)
def rerun_site(site_id: SiteIdPath) -> JSONResponse:
    """Queues a real job -- but an `onboarding`-kind one (existing contract §5.3 stages), not a
    `rerun`-kind job: `jobs.STAGES` has no `rerun` entry yet (docs/progress.md, open gap), so this
    does NOT skip terrain the way "re-run reuses existing terrain" implies. That needs its own
    decision (rerun stage list + contract §5.3 change) before it can be done honestly."""
    _require_known_site(site_id)
    try:
        cfg = site_registry.load(site_id)
    except (FileNotFoundError, SiteConfigError) as e:
        raise HTTPException(
            status_code=404,
            detail=mocks.error("site_not_found", f"No site config for '{site_id}': {e}", {"site_id": site_id}),
        ) from e
    conn = registry.connect()
    try:
        active = jobs.find_active_job(conn, site_id)
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail=mocks.error("site_onboarding_in_progress", f"Site '{site_id}' already has an active job.", {"site_id": site_id, "job_id": active}),
            )
        job_id = jobs.create_job(conn, "onboarding", site_id, payload={"site_config": cfg.model_dump(mode="json"), "rerun": True})
    finally:
        conn.close()
    return _validated_json("job_accepted.schema.json", {"job_id": job_id}, status_code=202)


# =============================================================================
# 9-10. POST /flood/query, GET /flood/{query_id}
# =============================================================================
@app.post(f"{API}/flood/query")
def query_flood(body: Annotated[dict, Body(...)]) -> JSONResponse:
    _validate_request_body("flood_query_request.schema.json", body)
    _require_known_site(body["site_id"])
    if (registry.data_dir() / body["site_id"] / "demo_ready.json").is_file():
        from datetime import datetime, timezone
        import secrets
        query_id = f"q_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"
        demo_cfg = load_site_config(body["site_id"], sites_dir=registry.data_dir() / body["site_id"] / "config")
        payload = __import__("backend.m0_api.synthetic_demo", fromlist=["create_query"]).create_query(
            body["site_id"], query_id, body, registry.data_dir(), list(demo_cfg.placeholder_fields))
        conn = registry.connect()
        try:
            with conn:
                conn.execute("INSERT INTO queries (query_id,site_id,request_json,status,result_path,created_at) VALUES (?,?,?,?,?,?)",
                    (query_id, body["site_id"], __import__("json").dumps(body), "complete",
                     str(registry.data_dir() / body["site_id"] / "queries" / query_id / "result.json"), registry.utc_now()))
        finally:
            conn.close()
    else:
        scenario_id = body.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise HTTPException(status_code=422, detail=mocks.error(
                "scenario_id_required", "Real scenario queries require a registered scenario_id."))
        if body["mode"] != "scenario":
            raise HTTPException(status_code=422, detail=mocks.error(
                "unsupported_direct_query_mode", "Direct registered runs support scenario mode only."))
        from datetime import datetime, timezone
        import secrets
        import json
        query_id = f"q_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"
        try:
            payload = real_query.resolve_registered_run(body["site_id"], scenario_id, body["model"],
                                                        query_id, body)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=mocks.error(
                "real_run_not_found", str(exc), {"site_id": body["site_id"], "scenario_id": scenario_id})) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=mocks.error("invalid_run_artifacts", str(exc))) from exc
        conn = registry.connect()
        try:
            with conn:
                conn.execute("INSERT INTO queries (query_id,site_id,request_json,status,result_path,created_at) VALUES (?,?,?,?,?,?)",
                    (query_id, body["site_id"], json.dumps(body), "complete",
                     str(registry.data_dir() / body["site_id"] / "queries" / query_id / "result.json"), registry.utc_now()))
        finally:
            conn.close()
    return _validated_json("flood_query_response.schema.json", payload)


@app.get(f"{API}/flood/{{query_id}}")
def get_flood(query_id: QueryIdPath) -> JSONResponse:
    conn = registry.connect()
    try:
        row = conn.execute("SELECT result_path FROM queries WHERE query_id=?", (query_id,)).fetchone()
    finally:
        conn.close()
    if row and row["result_path"] and __import__("pathlib").Path(row["result_path"]).is_file():
        return _validated_json("flood_query_response.schema.json", __import__("json").loads(__import__("pathlib").Path(row["result_path"]).read_text()))
    raise HTTPException(status_code=404, detail=mocks.error("query_not_found", f"No result artifact for query '{query_id}'.", {"query_id":query_id}))


# =============================================================================
# 11. GET /flood/{query_id}/layers/{layer_id}.png
# =============================================================================
@app.get(f"{API}/flood/{{query_id}}/layers/{{layer_filename}}")
def get_flood_layer(query_id: QueryIdPath, layer_filename: str) -> Response:
    if not layer_filename.endswith(".png"):
        raise HTTPException(status_code=400, detail=mocks.error("invalid_layer", f"'{layer_filename}' is not a .png layer request."))
    layer_id = layer_filename[: -len(".png")]
    # Contract §1.8: real GeoTIFFs land at data/<site_id>/queries/<query_id>/layers/.
    # M5 doesn't produce them yet, so this is a real render only when one has been
    # placed there by hand (e.g. a test); otherwise fall back to the mock PNG.
    site_dirs = [p.name for p in registry.data_dir().iterdir() if p.is_dir()] if registry.data_dir().is_dir() else []
    for site_id in site_dirs:
        tif_path = registry.data_dir() / site_id / "queries" / query_id / "layers" / f"{layer_id}.tif"
        if tif_path.is_file():
            try:
                png_bytes = rendering.render_and_cache(tif_path, layer_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=mocks.error("invalid_layer", str(exc))) from exc
            return Response(content=png_bytes, media_type="image/png")
    raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No generated raster layer '{layer_id}' for query '{query_id}'."))


# =============================================================================
# 12. GET /flood/{query_id}/extent.geojson
# =============================================================================
@app.get(f"{API}/flood/{{query_id}}/extent.geojson")
def get_flood_extent(query_id: QueryIdPath) -> JSONResponse:
    for site_dir in registry.data_dir().iterdir():
        path = site_dir / "queries" / query_id / "extent.geojson"
        if path.is_file():
            return _validated_json("geojson_feature_collection.schema.json", __import__("json").loads(path.read_text()))
    raise HTTPException(status_code=404, detail=mocks.error("query_not_found", f"No extent artifact for query '{query_id}'.", {"query_id":query_id}))


# =============================================================================
# 13. GET /flood/{query_id}/timeline
# =============================================================================
@app.get(f"{API}/flood/{{query_id}}/timeline")
def get_flood_timeline(query_id: QueryIdPath, interval_s: int = Query(300, ge=60, le=86400)) -> JSONResponse:
    found = api_timeline.find_query_timeline_dir(query_id)
    if found is None:
        # Direct solver queries may not have an M5 arrival ensemble. Build
        # their sparse playback frames from the retained FM map records.
        for site_dir in registry.data_dir().iterdir():
            result_path = site_dir / "queries" / query_id / "result.json"
            if not result_path.is_file():
                continue
            result = __import__("json").loads(result_path.read_text())
            run_ids = result.get("provenance", {}).get("run_ids", [])
            if result.get("method") not in ("delft3d_direct", "sph_direct") or not run_ids:
                break
            if result.get("method") == "delft3d_direct":
                try:
                    timeline_dir = real_timeline.create_timeline(site_dir, result_path.parent, run_ids[0])
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(status_code=404, detail=mocks.error(
                        "artifact_not_found", f"No usable real solver timeline for '{query_id}': {exc}",
                        {"query_id": query_id})) from exc
                found = (site_dir.name, timeline_dir)
            break
    if found is None:
        raise HTTPException(status_code=404, detail=mocks.error("artifact_not_found", f"No timeline artifacts for query '{query_id}'.", {"query_id":query_id}))
    site_id, timeline_dir = found
    response = api_timeline.build_response(site_id, timeline_dir, query_id, interval_s)
    if len(response["frames"]) > api_timeline.MAX_FRAMES:
        raise HTTPException(status_code=422, detail=mocks.error(
            "too_many_frames", f"{len(response['frames'])} frames exceeds the {api_timeline.MAX_FRAMES} limit; increase interval_s."))
    return _validated_json("timeline.schema.json", response)


# =============================================================================
# 14. GET /impact/{query_id}
# =============================================================================
@app.get(f"{API}/impact/{{query_id}}")
def get_impact(query_id: QueryIdPath) -> JSONResponse:
    for site_dir in registry.data_dir().iterdir():
        path = site_dir / "queries" / query_id / "impact.json"
        result_path = site_dir / "queries" / query_id / "result.json"
        if result_path.is_file():
            result = __import__("json").loads(result_path.read_text())
            if result.get("method") == "delft3d_direct":
                try:
                    impact = real_impact.build_impact(site_dir, result_path.parent)
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(status_code=404, detail=mocks.error(
                        "artifact_not_found", f"Cannot derive impact from registered solver artifacts: {exc}",
                        {"query_id": query_id})) from exc
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(__import__("json").dumps(impact, indent=2) + "\n", encoding="utf-8")
                return _validated_json("impact.schema.json", impact)
        if path.is_file():
            return _validated_json("impact.schema.json", __import__("json").loads(path.read_text()))
    raise HTTPException(status_code=404, detail=mocks.error("artifact_not_found", f"No impact artifact for query '{query_id}'.", {"query_id":query_id}))


# =============================================================================
# 15. GET /compare/{site_id}?scenario_id=
# =============================================================================
@app.get(f"{API}/compare/{{site_id}}")
def get_compare(site_id: SiteIdPath, scenario_id: str | None = Query(default=None)) -> JSONResponse:
    _require_known_site(site_id)
    # The existing Model Comparison page requests the site's default comparison
    # without a scenario parameter. Expose the real Teesta MVP pair there once
    # that sidecar exists; explicit scenario requests remain exact.
    mvp_scenario = scenario_id or ("teesta_2023_mvp" if site_id == "teesta" else None)
    mvp_pair = registry.data_dir() / site_id / "compare" / (mvp_scenario or "") / "compare.json"
    if mvp_scenario and mvp_pair.is_file():
        return _validated_json("compare.schema.json", __import__("json").loads(mvp_pair.read_text()))
    found = api_compare.find_compare_sidecar(site_id, scenario_id)
    if found is not None:
        model, held_out_run_id, sidecar_path = found
        response = api_compare.build_response(site_id, scenario_id, model, held_out_run_id, sidecar_path)
        return _validated_json("compare.schema.json", response)
    if (registry.data_dir() / site_id / "demo_ready.json").is_file():
        run_meta_path = registry.data_dir() / site_id / "runs" / f"{site_id}_demo_s001__synthetic" / "run_meta.json"
        if not run_meta_path.is_file():
            raise HTTPException(status_code=404, detail=mocks.error("artifact_not_found", f"Synthetic run metadata missing for '{site_id}'."))
        synthetic_run = __import__("json").loads(run_meta_path.read_text())
        response = {"site_id":site_id,"scenario_id":scenario_id or synthetic_run["scenario_id"],
          "sph_vs_delft3d":{"available":False,"domain":"nearfield","time_window_s":0,"metrics":{},"probes":[],"layers":[],"run_ids":[]},
          "emulator_vs_physics":{"available":False,"held_out_run_id":None,"metrics":{},"layers":[]},"gp_vs_linear":{},
          "when_to_use_key":"compare_synthetic_demo_unavailable","caveats":[{ "id":"synthetic_demo","severity":"warning","text_key":"caveat_synthetic_demo"}]}
        return _validated_json("compare.schema.json", response)
    # Never decorate the real Teesta MVP run with the contract example's zero
    # scores / fictitious paired run IDs. A direct comparison is available only
    # when a real pair artifact has been written by the comparison pipeline.
    real_runs_dir = registry.data_dir() / site_id / "runs"
    real_run_meta = []
    if real_runs_dir.is_dir():
        for meta_path in real_runs_dir.glob("*/run_meta.json"):
            try:
                meta = __import__("json").loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("solver_status") == "REAL_SOLVER_OUTPUT" or meta.get("output_classification") == "REAL_SIMULATION_ARTIFACT":
                if scenario_id is None or meta.get("scenario_id") == scenario_id:
                    real_run_meta.append(meta)
    if real_run_meta:
        run_meta = real_run_meta[0]
        response = {
            "site_id": site_id,
            "scenario_id": scenario_id or run_meta.get("scenario_id", "unpaired_real_run"),
            "sph_vs_delft3d": {"available": False, "domain": "nearfield", "time_window_s": 0,
                               "metrics": {}, "probes": [], "layers": [],
                               "run_ids": [run_meta["run_id"]] if run_meta.get("run_id") else []},
            "emulator_vs_physics": {"available": False, "held_out_run_id": None, "metrics": {}, "layers": []},
            "gp_vs_linear": {}, "when_to_use_key": "comparison_unavailable",
            "caveats": [{"id": "comparison_unavailable", "severity": "warning",
                         "text_key": "comparison_unavailable"}],
        }
        return _validated_json("compare.schema.json", response)
    if scenario_id:
        conn = registry.connect()
        try:
            has_real_run = conn.execute("SELECT 1 FROM runs WHERE scenario_id=? AND model IN ('delft3d','sph') AND status IN ('completed','postprocessed') LIMIT 1",
                                        (scenario_id,)).fetchone() is not None
        finally:
            conn.close()
        if has_real_run:
            raise HTTPException(status_code=404, detail=mocks.error("artifact_not_found", f"No comparison artifact for registered scenario '{scenario_id}'.", {"scenario_id":scenario_id}))
    ids = {"site_id": site_id}
    if scenario_id:
        ids["scenario_id"] = scenario_id
    return _validated_json("compare.schema.json", mocks.mock_response("compare.example.json", **ids))


# =============================================================================
# 16-17. GET /validation/{site_id}[?event=]
# =============================================================================
@app.get(f"{API}/validation/{{site_id}}")
def get_validation(site_id: SiteIdPath, event: str | None = Query(default=None)) -> JSONResponse:
    _require_known_site(site_id)
    if (registry.data_dir() / site_id / "demo_ready.json").is_file():
        if event:
            payload = {"contract_version":"0.3.0","site_id":site_id,"event_id":event,"observed":{},"predicted":{},"metrics":{},
                       "comparison_domain":"none","caveats":[{"id":"synthetic_demo","severity":"warning","text_key":"caveat_synthetic_demo"}],
                       "provenance":{"method":"empirical_fallback","contract_version":"0.3.0","synthetic":True,"validation_available":False}}
            return _validated_json("historical_validation.schema.json", payload)
        payload = {"contract_version":"0.3.0","site_id":site_id,"model":"delft3d","n_runs":0,"per_run":[],"summary":{},
                   "baseline_linear":{},"grade_thresholds_ref":"docs/m5_specs.md","events":[],"synthetic_demo":True,
                   "validation_available":False,"note":"I-1 tests software plumbing; it contains no scientific validation data."}
        return _validated_json("validation.schema.json", payload)
    # A Teesta solver run is not M5 validation. Return the contract's empty
    # report shape unless an actual validation report exists on disk.
    validation_root = registry.data_dir() / site_id / "emulator" / "delft3d" / "validation"
    real_report = validation_root / ("historical.json" if event else "loocv.json")
    if real_report.is_file():
        payload = __import__("json").loads(real_report.read_text())
        schema = "historical_validation.schema.json" if event else "validation.schema.json"
        return _validated_json(schema, payload)
    real_runs_dir = registry.data_dir() / site_id / "runs"
    real_run_meta: dict | None = None
    real_run_dir = None
    if real_runs_dir.is_dir():
        for meta_path in real_runs_dir.glob("*/run_meta.json"):
            try:
                meta = __import__("json").loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("solver_status") == "REAL_SOLVER_OUTPUT" or meta.get("output_classification") == "REAL_SIMULATION_ARTIFACT":
                real_run_meta, real_run_dir = meta, meta_path.parent
                break
    has_real_run = real_run_meta is not None
    try:
        site_events = [e.id for e in load_site_config(site_id).events]
    except (FileNotFoundError, SiteConfigError):
        site_events = []
    if has_real_run and not event:
        payload = {"contract_version": "0.3.0", "site_id": site_id, "model": "delft3d",
                   "n_runs": 0, "per_run": [], "summary": {}, "baseline_linear": {},
                   "grade_thresholds_ref": "docs/m5_specs.md", "events": site_events,
                   "synthetic_loocv": validation_helpers.synthetic_loocv_summary()}
        return _validated_json("validation.schema.json", payload)
    if has_real_run and event and event in site_events:
        assert real_run_meta is not None and real_run_dir is not None
        data_dir = registry.data_dir()
        observed = validation_helpers.observed_extent_status(site_id, event, data_dir)
        literature = validation_helpers.build_literature_comparison(site_id, real_run_meta, data_dir)
        predicted_area = validation_helpers.build_predicted_extent(real_run_meta, real_run_dir)
        extent_metrics = None
        if observed["available"]:
            observed_path = validation_helpers.resolve_observed_extent_path(site_id, event, data_dir)
            extent_metrics = validation_helpers.build_extent_metrics(observed_path, real_run_meta, real_run_dir)
        caveats = [{"id": "no_observed_extent", "severity": "warning",
                    "text_key": "no_observed_extent"}] if not observed["available"] else []
        if literature["available"]:
            caveats.append({"id": "literature_comparison_only", "severity": "info",
                             "text_key": "literature_comparison_only"})
        caveats.append({"id": "clear_water", "severity": "warning", "text_key": "caveat_clear_water"})
        payload = {"contract_version": "0.3.0", "site_id": site_id, "event_id": event,
                   "observed": observed,
                   "predicted": ({"delft3d_direct": {"area_m2": predicted_area}} if predicted_area else {}),
                   "metrics": ({"delft3d_direct": extent_metrics} if extent_metrics else {}),
                   "comparison_domain": "point comparison at Chungthang against literature reconstructions"
                                        if literature["available"] else "none",
                   "caveats": caveats,
                   "provenance": {"method": "real_run_vs_literature", "contract_version": "0.3.0",
                                  "run_id": real_run_meta.get("run_id"),
                                  "validation_available": bool(observed["available"] or literature["available"])},
                   "literature_comparison": literature}
        return _validated_json("historical_validation.schema.json", payload)
    if has_real_run and event:
        # A registered solver run against an event this site doesn't declare is not a real
        # validation pair. Avoid leaking the contract example's zero scores into real-site mode.
        payload = {"contract_version": "0.3.0", "site_id": site_id, "event_id": event,
                   "observed": {}, "predicted": {}, "metrics": {}, "comparison_domain": "none",
                   "caveats": [{"id": "validation_unavailable", "severity": "warning",
                                "text_key": "validation_unavailable"}],
                   "provenance": {"method": "none", "contract_version": "0.3.0",
                                  "validation_available": False}}
        return _validated_json("historical_validation.schema.json", payload)
    if event:
        payload = mocks.mock_response("historical_validation.example.json", site_id=site_id, event_id=event)
        return _validated_json("historical_validation.schema.json", payload)
    payload = mocks.mock_response("validation.example.json", site_id=site_id)
    return _validated_json("validation.schema.json", payload)


# =============================================================================
# 18. GET /export/{query_id}?format=shp|kml|geojson|pdf
# =============================================================================
_EXPORT_MEDIA_TYPES = {
    "shp": "application/zip",
    "kml": "application/vnd.google-earth.kml+xml",
    "geojson": "application/geo+json",
    "pdf": "application/pdf",
}


def _bounds_lonlat(bounds: list[list[float]] | None) -> list[list[float]] | None:
    """Converts a contract-standard `bounds_latlng` (§1.3, `[[south,west],[north,east]]`) into
    the `[[west,east],[south,north]]` pairs `exports.build_pdf_report`'s map panel expects
    (matplotlib's `imshow(extent=...)` order). `real_query.py`/`real_timeline.py` used to emit
    `bounds_latlng` axis-swapped, which this function once had to compensate for by trusting a
    known producer bug rather than the field's documented order; that bug is now fixed at the
    source (docs/progress.md 2026-09-28 "STEP 2"), so this is a plain, unconditional reorder."""
    if not bounds:
        return None
    (south, west), (north, east) = bounds
    return [[west, east], [south, north]]


@app.get(f"{API}/export/{{query_id}}")
def export_query(query_id: QueryIdPath, format: str = Query(...)) -> Response:  # noqa: A002 - contract's param name
    if format not in _EXPORT_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=mocks.error("invalid_format", f"format must be one of {sorted(_EXPORT_MEDIA_TYPES)}, got '{format}'."),
        )
    import json
    from pathlib import Path

    from backend.m6_impact import exports as m6_exports

    conn = registry.connect()
    try:
        query = conn.execute("SELECT site_id,result_path FROM queries WHERE query_id=?", (query_id,)).fetchone()
    finally:
        conn.close()
    if query and query["result_path"] and Path(query["result_path"]).is_file():
        site_id = query["site_id"]
        result_path = Path(query["result_path"])
        query_dir = result_path.parent
        extent_path = query_dir / "extent.geojson"
        if not extent_path.is_file():
            raise HTTPException(status_code=404, detail=mocks.error("artifact_not_found", f"Extent artifact missing for query '{query_id}'."))
        extent = json.loads(extent_path.read_text())
        query_result = json.loads(result_path.read_text())
        is_synthetic = bool(query_result.get("flags", {}).get("demo_mode"))
        report_label = "Synthetic I-1 report" if is_synthetic else "Direct solver result"
        method = query_result.get("method", "unknown")
        caveats = query_result.get("caveats", [])
        has_placeholders = bool(query_result.get("flags", {}).get("has_placeholders"))
        provenance = query_result.get("provenance", {})
        run_ids = provenance.get("run_ids", [])
        confidence_level = (query_result.get("confidence", {}) or {}).get("extent", {}).get("level")
        # The single deterministic extent a direct run produces is placed in the POSSIBLE
        # bucket for exposure purposes (`real_impact.py`'s own documented convention, echoed in
        # its `data_coverage_notes`); the emulator path isn't reachable for any registered
        # query yet, so there's no HIGH/POSSIBLE split to read there instead.
        zone = "possible" if method.endswith("_direct") else None

        impact_path = query_dir / "impact.json"
        impact = json.loads(impact_path.read_text()) if impact_path.is_file() else None
        warning_table = (impact or {}).get("warning_table", [])

        poi_locations: dict[str, tuple[float, float]] = {}
        site_name = site_id
        try:
            site_config = load_site_config(site_id)
            poi_locations = m6_exports.poi_locations_from_site_config(site_config)
            site_name = site_config.site.name
        except (SiteConfigError, FileNotFoundError):
            pass

        if format == "geojson":
            content, filename = json.dumps(extent).encode(), f"{site_id}_{query_id}_extent.geojson"
        elif format == "kml":
            description = None
            summary = query_result.get("summary", {})
            if summary:
                caveats_text = "; ".join(m6_exports.caveat_label(c) for c in caveats) or "none"
                description = (
                    f"Max depth: {(summary.get('max_depth_m') or {}).get('value')} m<br/>"
                    f"Max velocity: {(summary.get('max_velocity_ms') or {}).get('value')} m/s<br/>"
                    f"Caveats: {caveats_text}"
                )
            # contracts/styles.json extent_class.{high,possible} -- same fill this extent gets on
            # the Leaflet map, so the KML doesn't invent its own colour scheme.
            zone_fill, zone_opacity = {"high": ("#d7263d", 0.65), "possible": ("#f4a259", 0.35)}.get(
                zone, ("#f4a259", 0.35))
            kml_doc = m6_exports.extent_geojson_to_kml(extent, f"{report_label} {query_id}",
                                                         fill_hex=zone_fill, opacity=zone_opacity,
                                                         description=description)
            pois_folder = m6_exports.pois_kml_folder(warning_table, poi_locations)
            if pois_folder:
                kml_doc = kml_doc.replace("</Document></kml>", pois_folder + "</Document></kml>")
            content = kml_doc.encode()
            filename = f"{site_id}_{query_id}_extent.kml"
        elif format == "shp":
            content = m6_exports.build_shapefile_zip(
                extent, site_id=site_id, query_id=query_id, run_ids=run_ids, method=method,
                zone=zone, confidence_level=confidence_level, has_placeholders=has_placeholders,
                caveats=caveats, summary=query_result.get("summary", {}), warning_table=warning_table,
                poi_locations=poi_locations,
            )
            filename = f"{site_id}_{query_id}_extent.zip"
        else:
            map_png_bytes, map_bounds, map_label = None, None, None
            depth_tif = query_dir / "layers" / "depth_p50.tif"
            if depth_tif.is_file():
                try:
                    map_png_bytes = rendering.render_and_cache(depth_tif, "depth_p50")
                    depth_layer = next((l for l in query_result.get("layers", []) if l.get("layer_id") == "depth_p50"), None)
                    map_bounds = _bounds_lonlat(depth_layer["bounds_latlng"]) if depth_layer else None
                    map_label = "Depth (P50), m"
                except ValueError:
                    pass
            content = m6_exports.build_pdf_report(
                site_id=site_id, site_name=site_name, query_id=query_id, report_label=report_label,
                is_synthetic=is_synthetic, summary=query_result.get("summary", {}), impact=impact,
                caveats=caveats, provenance=provenance, has_placeholders=has_placeholders,
                map_png_bytes=map_png_bytes, map_bounds_latlng=map_bounds, map_label=map_label,
            )
            filename = f"{site_id}_{query_id}_report.pdf"
        return Response(content=content, media_type=_EXPORT_MEDIA_TYPES[format], headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    # No registered query, real or otherwise, exists for this id: a real site's export must
    # never be fabricated content branded with someone's actual query_id (mock_files' shapefile/
    # KML/PDF text literally say "site=teesta query=<id>", which reads as a real, computed export
    # if someone doesn't notice the "mock" wording). Always 404, naming the missing id.
    raise HTTPException(status_code=404, detail=mocks.error(
        "query_not_found", f"No registered query artifacts for '{query_id}'.", {"query_id": query_id}))


# =============================================================================
# 19-20. GET /gee/{site_id}, POST /gee/{site_id}/refresh
# =============================================================================
def _gee_layers_or_mock(site_id: str) -> dict:
    """Real cache (`data/<site_id>/gee/`) once M7 has actually fetched something for this site
    (`gee_meta.json` exists, so `fetched_at` is set); otherwise the contract's mock, same as every
    other still-mocked endpoint (module docstring)."""
    layers = gee_cache.load_layers(site_id, data_dir=registry.data_dir())
    if layers.get("fetched_at") is None:
        return mocks.mock_response("gee_layers.example.json", site_id=site_id)
    return layers


@app.get(f"{API}/gee/{{site_id}}")
def get_gee(site_id: SiteIdPath) -> JSONResponse:
    _require_known_site(site_id)
    return _validated_json("gee_layers.schema.json", _gee_layers_or_mock(site_id))


@app.post(f"{API}/gee/{{site_id}}/refresh")
def refresh_gee(site_id: SiteIdPath) -> JSONResponse:
    """Tries a live Earth Engine fetch (lake area/rainfall via `gee_fetch.run`, event imagery via
    `gee_imagery.refresh`); either falls back to the existing cache on its own on any failure --
    missing/expired credentials, no network, no usable scene -- so this handler never 500s over a
    live-fetch problem, only over `site_id` not being configured at all."""
    _require_known_site(site_id)
    data_dir = registry.data_dir()
    live_ok = False
    try:
        cfg = load_site_config(site_id)
        provider = gee_fetch.best_effort_provider()
        lake_result = gee_fetch.run(site_id, provider=provider, data_dir=data_dir)
        img_result = gee_imagery.refresh(site_id, cfg=cfg, data_dir=data_dir)
        live_ok = not lake_result.errors and img_result.source == "live"
    except SiteConfigError as e:
        log.warning("refresh_gee: site config error for '%s', serving existing cache: %s", site_id, e)
    except Exception as e:  # never let a refresh attempt take the endpoint down
        log.warning("refresh_gee: unexpected error for '%s', serving existing cache: %s", site_id, e)

    payload = _gee_layers_or_mock(site_id)
    if live_ok:
        payload["source"] = "live"
    return _validated_json("gee_layers.schema.json", payload)


# =============================================================================
# 21. GET /scene3d/{query_id}
# =============================================================================
@app.get(f"{API}/scene3d/{{query_id}}")
def get_scene3d(
    query_id: QueryIdPath,
    vertical_exaggeration: float = Query(api_scene3d.DEFAULT_VERTICAL_EXAGGERATION, ge=0.1, le=10.0),
) -> JSONResponse:
    try:
        payload = api_scene3d.build_scene(query_id, vertical_exaggeration)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=mocks.error("scene_too_large", str(exc))) from exc
    if payload is None:
        payload = mocks.mock_response("scene3d.example.json", query_id=query_id)
        payload["frame"]["vertical_exaggeration"] = vertical_exaggeration
    return _validated_json("scene3d.schema.json", payload)


#: A timeline frame PNG's path, anchored end to end so no other shape of
#: `path` (in particular nothing with `..` or extra segments) can match.
TIMELINE_FRAME_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/queries/(?P<query_id>q_\d{{8}}T\d{{6}}Z_[0-9a-f]{{6}})"
    r"/timeline/(?P<band>median|high|possible)_t(?P<t_s>\d+)\.png$"
)

#: A Compare depth-difference PNG's path, anchored the same way (run_id
#: restricted to id-safe characters -- no `/` or `.` -- so nothing can
#: escape `validation/compare/`).
COMPARE_DIFF_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/emulator/(?P<model>delft3d|sph)/validation/compare/"
    r"(?P<run_id>[A-Za-z0-9_]+)__depth_diff\.png$"
)

#: An M7 event-imagery PNG under `data/<site_id>/gee/imagery/` (`gee_imagery.convert`), anchored
#: the same way so nothing can escape that directory.
GEE_IMAGERY_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/gee/imagery/(?P<filename>[A-Za-z0-9_]+\.png)$"
)

#: The canonical observed-extent GeoJSON under `data/<site_id>/observed/` (`observed.convert`),
#: anchored the same way so nothing can escape that directory. Not under `gee/`: this is an
#: operator-digitized artifact, not an Earth Engine fetch product (docs/handoff_contract.md §1.8,
#: updated 2026-09-28).
OBSERVED_EXTENT_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/observed/(?P<filename>flood_extent_\d{{4}}\.geojson)$"
)

SCENE_ASSET_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/queries/(?P<query_id>q_\d{{8}}T\d{{6}}Z_[0-9a-f]{{6}})"
    r"/scene3d/(?P<filename>terrain\.bin|flood_surface\.bin|delft3d_surface\.glb)$"
)
SPH_SURFACE_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/runs/(?P<run_id>[A-Za-z0-9_]+__sph)/surfaces/(?P<filename>t\d+\.glb)$"
)
MVP_COMPARE_DIFF_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/compare/(?P<scenario_id>[A-Za-z0-9_]+)/depth_diff\.png$"
)

#: A run's own `run_meta.json` (contract §1.8 file layout) -- read-only passthrough, same
#: pattern as the other real-artifact routes below. Powers the 3D view's breach-parameter
#: tooltip (docs/progress.md 2026-09-28 "3D view: water surface + breach marker").
RUN_META_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/runs/(?P<run_id>[A-Za-z0-9_]+__(delft3d|sph))/run_meta\.json$"
)
#: An M2 breach hydrograph's forcing-provenance sidecar (`breach/hydrographs/*.json`).
BREACH_HYDROGRAPH_PROVENANCE_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/breach/hydrographs/(?P<filename>[A-Za-z0-9_]+\.json)$"
)
#: M2's per-site `breach_params.json`, when it has actually been written to disk.
BREACH_PARAMS_PATH_RE = re.compile(
    rf"^(?P<site_id>{SITE_ID_PATTERN[1:-1]})/breach/breach_params\.json$"
)


# =============================================================================
# 22. GET /files/{path}
# =============================================================================
@app.get(f"{API}/files/{{path:path}}")
def get_file(path: str) -> Response:
    m = MVP_COMPARE_DIFF_PATH_RE.match(path)
    if m is not None:
        sidecar = registry.data_dir() / m["site_id"] / "compare" / m["scenario_id"] / "compare.json"
        if not sidecar.is_file():
            raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No paired comparison at '{path}'.", {"path": path}))
        from backend.m4_sph.compare_mvp import render_mvp_depth_diff
        return Response(content=render_mvp_depth_diff(registry.data_dir(), m["scenario_id"]), media_type="image/png")

    m = SCENE_ASSET_PATH_RE.match(path)
    if m is not None:
        asset_path = registry.data_dir() / m["site_id"] / "queries" / m["query_id"] / "scene3d" / m["filename"]
        if asset_path.is_file():
            media_type = "model/gltf-binary" if asset_path.suffix == ".glb" else "application/octet-stream"
            return Response(content=asset_path.read_bytes(), media_type=media_type)
        if m["filename"] in {"terrain.bin", "flood_surface.bin"}:
            # Contract example grid is 500 x 375 float32 samples (750,000 bytes).
            return Response(content=b"\x00" * 750_000, media_type="application/octet-stream")
        raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No scene asset at '{path}'.", {"path": path}))

    m = SPH_SURFACE_PATH_RE.match(path)
    if m is not None:
        asset_path = registry.data_dir() / m["site_id"] / "runs" / m["run_id"] / "surfaces" / m["filename"]
        if asset_path.is_file():
            return Response(content=asset_path.read_bytes(), media_type="model/gltf-binary")
        raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No SPH surface at '{path}'.", {"path": path}))

    m = TIMELINE_FRAME_PATH_RE.match(path)
    if m is not None:
        timeline_dir = registry.data_dir() / m["site_id"] / "queries" / m["query_id"] / "timeline"
        if (timeline_dir / "timeline_data.json").is_file():
            png_bytes = api_timeline.render_frame(timeline_dir, m["band"], int(m["t_s"]))
            return Response(content=png_bytes, media_type="image/png")
        if (registry.data_dir() / m["site_id"] / "demo_ready.json").is_file():
            raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No timeline frame at '{path}'.", {"path":path}))

    m = COMPARE_DIFF_PATH_RE.match(path)
    if m is not None:
        sidecar_path = (
            registry.data_dir() / m["site_id"] / "emulator" / m["model"] / "validation" / "compare" / f"{m['run_id']}.json"
        )
        if sidecar_path.is_file():
            png_bytes = api_compare.render_diff_layer(sidecar_path, m["run_id"])
            return Response(content=png_bytes, media_type="image/png")

    m = GEE_IMAGERY_PATH_RE.match(path)
    if m is not None:
        png_path = registry.data_dir() / m["site_id"] / "gee" / "imagery" / m["filename"]
        if png_path.is_file():
            return Response(content=png_path.read_bytes(), media_type="image/png")
        raise HTTPException(
            status_code=404,
            detail=mocks.error("file_not_found", f"No GEE imagery file at '{png_path}'.", {"path": path}),
        )

    m = OBSERVED_EXTENT_PATH_RE.match(path)
    if m is not None:
        geojson_path = registry.data_dir() / m["site_id"] / "observed" / m["filename"]
        if geojson_path.is_file():
            return Response(content=geojson_path.read_bytes(), media_type="application/geo+json")
        raise HTTPException(
            status_code=404,
            detail=mocks.error("file_not_found", f"No observed-extent file at '{geojson_path}'.", {"path": path}),
        )

    m = RUN_META_PATH_RE.match(path)
    if m is not None:
        meta_path = registry.data_dir() / m["site_id"] / "runs" / m["run_id"] / "run_meta.json"
        if meta_path.is_file():
            return Response(content=meta_path.read_bytes(), media_type="application/json")
        raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No run_meta.json at '{path}'.", {"path": path}))

    m = BREACH_HYDROGRAPH_PROVENANCE_PATH_RE.match(path)
    if m is not None:
        sidecar_path = registry.data_dir() / m["site_id"] / "breach" / "hydrographs" / m["filename"]
        if sidecar_path.is_file():
            return Response(content=sidecar_path.read_bytes(), media_type="application/json")
        raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No breach hydrograph sidecar at '{path}'.", {"path": path}))

    m = BREACH_PARAMS_PATH_RE.match(path)
    if m is not None:
        params_path = registry.data_dir() / m["site_id"] / "breach" / "breach_params.json"
        if params_path.is_file():
            return Response(content=params_path.read_bytes(), media_type="application/json")
        # Unlike run_meta.json/forcing provenance (expected once a run is registered), M2's
        # breach_params.json is often genuinely never written for a site (matches the existing
        # `available: false` convention for Compare, rather than a 404 -- this artifact's
        # absence is an expected, not exceptional, state for callers like the breach tooltip.
        import json as _json
        return Response(content=_json.dumps({"available": False}).encode(), media_type="application/json")

    if path.endswith(".png"):
        return Response(content=mock_files.mock_png(), media_type="image/png")
    if path.endswith(".geojson"):
        import json

        return Response(content=json.dumps(mocks.mock_response("geojson_feature_collection.example.json")).encode(), media_type="application/geo+json")
    if path.endswith(".bin"):
        # Mock float32 payload for Scene3D terrain/flood_surface binaries (contract §5.9).
        return Response(content=b"\x00\x00\x00\x00" * 16, media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail=mocks.error("file_not_found", f"No mock file for '{path}'."))

# Team decisions

## 2026-09-28 — M7 GEE fetch: fix the latest/latest_valid_date match

`lake_latest.geojson` was empty on disk for the real Teesta site despite `lake_area.csv` having
many valid rows. Root cause: `_fetch_lake_area`'s old logic only ever tracked a component (the
actual polygon array) for months processed IN THAT RUN's loop -- which, once a site has any cache
at all, is only `REFETCH_TRAILING_MONTHS` (2) months, since everything else is skipped as already
cached (`if date_str in cached_dates and date_str not in always_refetch: continue`). It then
required the freshly-tracked month's date to exactly equal the globally latest valid date across
*all* merged rows (fresh + cached). Whenever both trailing months skipped (cloud/snow/no scene --
common in a monsoon-affected Himalayan series) while an older, already-cached month held the true
latest valid area, the dates could never match, `latest` stayed `None`, and (since `run()` only
ever writes the placeholder `{}` once, the first time no file exists) the polygon silently stayed
stale or empty on every subsequent run -- indefinitely, since nothing ever resets it back to a
populated state once it's been through this path.

**Fix:** `_fetch_lake_area` now always resolves the globally latest valid date from `merged`
first. If that month was processed fresh this run, its component is reused as before. If it
wasn't (the bug's exact scenario), the month is recomputed directly -- one extra `_classify_and_
mask` call, at most, regardless of `months_back` -- instead of being left stale. If even that
recompute can't produce a component (e.g. the provider no longer has anything for that month),
`lake_latest.geojson` is left exactly as it was rather than fabricating a polygon or crashing the
fetch. Extracted the per-month classify+mask+component logic (previously duplicated intent inline)
into `_classify_and_mask()` so both the main loop and this fallback path share one implementation.

Tests: `tests/m7_gee/test_fetch.py` -- `test_lake_latest_reflects_a_cached_valid_month_when_the_
newest_months_skip` (the bug's exact scenario: two clean runs, trailing months skip on the second,
`lake_latest.geojson` must still show the true latest valid month, not stay stale/empty) and
`test_lake_latest_stays_as_is_if_the_latest_valid_cached_month_cannot_be_recomputed` (the
un-recomputable case: no crash, file left untouched). `tests/m7_gee`: 121 passed (2 new), the
existing `test_lake_latest_geojson_is_the_newest_valid_month` (first-run case) still green,
confirming the refactor is behaviour-preserving there. Full `tests/m0_api`: 206 passed, 1
pre-existing hardware-gated skip, no regressions.

## 2026-09-28 — M7 GEE fetch: mask slopes steeper than ~6 degrees for S1 months

The Sept 2023 South Lhonak overestimate (4,348,200 m² vs ISRO/NRSC's 167.4 ha) used
`method: s1_threshold` (S2 was too cloudy that monsoon month). S1 VV backscatter over steep
Himalayan terrain suffers radar shadow: slopes facing away from the sensor return near-zero
signal, indistinguishable from smooth open water in the `below=True` mask -- already documented
via `fetch.py`'s existing `radar_shadow` caveat, which stays (this mitigates the risk, it does not
eliminate the physical cause, and the DEM/registration themselves carry uncertainty).

**Fix:** a lake surface is flat; radar shadow is not. Added `lake_area.slope_deg_from_elevation`
(pure numpy central-difference gradient -> degrees) and `lake_area.dem_slope_deg` (reprojects/
resamples a DEM file onto the AOI grid via `rasterio.warp.reproject`, then calls the pure
function). `water_mask` gained optional `slope_deg`/`max_slope_deg` args, same pattern as the NIR
args above: a pixel only counts as water if slope is at or below `max_slope_deg`. New
`GeeSettings.max_slope_deg = 6.0` (the value given in the task instruction, not tuned against
ISRO's numbers), applied only to `s1_threshold` months.

**Temporary DEM source, until ITEM 1 lands:** `fetch._dem_path(site_id, data_dir)` currently points
at `data/<site_id>/terrain/dem.tif` -- whatever M1 already wrote for the flood-domain pipeline, not
necessarily on the grid/provenance ITEM 1's canonical terrain artifact will settle on. Swapping the
source later needs only a change to `_dem_path`'s return path -- `dem_slope_deg` already reprojects
whatever it is given onto the AOI grid, so nothing else in this fix depends on the DEM's own
resolution or exact alignment. If no DEM file exists yet for a site, slope masking is skipped for
that fetch (logged, not raised) -- `_load_slope_deg` must never fail the whole fetch over a missing
or unreadable DEM.

No threshold changed apart from adding this one new, task-specified value.

Tests: `tests/m7_gee/test_lake_area.py::TestSlopeMasking` (pure gradient math against a known tilt
angle, `water_mask` slope-exclusion, a real rasterio reproject round trip) and two `fetch.py`
end-to-end tests (`TestRunEndToEnd::test_s1_month_applies_dem_slope_masking_when_a_dem_is_present`
-- a monkeypatched all-steep slope zeroes the S1 area, proving the wiring, not re-testing the
raster math; `test_s1_month_skips_slope_masking_gracefully_with_no_dem` -- fetch still succeeds
with no DEM present). `tests/m7_gee`: 119 passed (9 new), no regressions.

## 2026-09-28 — M7 GEE fetch: search each month for the clearest S2 scene

`EarthEngineProvider.s2_month` used to `.median()`-composite every Sentinel-2 scene found in a
calendar month before computing NDWI/cloud coverage. A month with one genuinely clear scene and
several cloudy ones then reported an averaged-down `valid_pct` (mean cloud-free fraction across
the whole stack), which could fall below `GeeSettings.max_cloud_pct` even though a single usable
scene existed — spuriously tripping `fetch.py`'s fallback to Sentinel-1 (and its radar-shadow
risk, see the entries above) for a month S2 could actually have covered.

**Fix:** added `pick_clearest_scene()` (pure, no `ee` needed) — picks the scene_id with the lowest
AOI cloud fraction from a `{scene_id: cloud_pct}` mapping. `s2_month` now computes each scene's SCL
cloud fraction over the AOI via a single batched `reduceRegion` per scene (client-side loop
avoided — one `ImageCollection.map().getInfo()` call), picks the clearest scene, and extracts
NDWI/NIR/valid/ice directly from that one image — no compositing. `scene_ids`/`acquisition_dates`
now report the single chosen scene, not every scene GEE found that month, which is also more
honest about exactly which pixels the reported area came from.

No threshold changed (`max_cloud_pct`, `ndwi_threshold_clamp`, etc. are untouched) — this only
changes which scene the existing thresholds are evaluated against.

Tested via `pick_clearest_scene` directly (pure function, 4 cases: lowest wins, empty returns
`None`, single scene, deterministic tie-break) — the same pattern `walk_upstream_basin_ids` already
uses to keep `EarthEngineProvider`'s EE-dependent methods testable without a live session per that
module's own docstring. `tests/m7_gee`: 110 passed (4 new), no regressions.

## 2026-09-28 — M7 GEE fetch: NIR test excludes snow from S2 water mask

Diagnosed the 2026-09-28 live Teesta fetch's South Lhonak lake-area overestimate (4,348,200 m² /
1,493,200 m² for Sep/Oct 2023 vs. ISRO/NRSC's 167.4 ha / 60.3 ha for the same two events, `src_072`
in `docs/data_sources.md` — about 2.6x). `backend/m7_gee/fetch.py` already restricted each month's
water mask to the single connected component containing the seeded dam location
(`lake_area.seed_component`), so the failure mode is not "the AOI includes an unrelated lake" as a
separate blob — it is a bright-NIR (snow/ice) corridor within the AOI whose NDWI still clears the
Otsu threshold, bridging the seeded lake to whatever else the corridor touches into one connected
component. `GeeSettings.max_snow_ice_pct` only skips a month if snow/ice covers >30% of the *whole*
AOI buffer; a smaller patch that happens to sit between the lake and something else was never
filtered at the pixel level.

**Fix:** `backend/m7_gee/provider.py` `s2_month` now also fetches B8 (NIR) surface reflectance
(scaled from the S2_SR_HARMONIZED 0-10000 DN to 0-1) alongside NDWI. `lake_area.water_mask` takes
optional `nir`/`nir_max` and additionally requires `nir <= nir_max` — open water is dark in the NIR
(<0.1 reflectance); snow/ice is bright (>0.5). New setting `GeeSettings.nir_reflectance_max = 0.15`
(`backend/m7_gee/settings.py`), applied only to `s2_water_index` months (Sentinel-1 has no NIR
band). `SyntheticProvider` gained a `snow_bridge_months` fixture (a bright-NIR corridor connecting
the seeded lake disc to a second, separate disc) so the exact bug is covered end to end
(`tests/m7_gee/test_lake_area.py::TestNirFiltersSnow`,
`tests/m7_gee/test_fetch.py::test_snow_bridge_does_not_inflate_area_via_nir_filter`).

**Not done this session:** a live re-fetch to report the corrected South Lhonak numbers. This
session's tool access could not run the backend (`Bash`/subagent calls were failing with a
server-side classifier error for the whole session), so the fix is verified by synthetic
regression tests and manual trace of the mask/component logic only, not against real Earth Engine
imagery. Whoever re-runs `python -m backend.m7_gee.fetch teesta --months 37` next should record the
new Sep/Oct 2023 area rows here.

**Also diagnosed separately (does not touch this fix):** the Sept 2023 row uses `s1_threshold`
(Sentinel-1 SAR), not `s2_water_index` — S2 was too cloudy for that monsoon-season month. S1 VV
backscatter over steep Himalayan terrain suffers radar shadow (slopes facing away from the sensor
return near-zero signal, indistinguishable from smooth open water in the `below=True` mask), which
`fetch.py`'s own `radar_shadow` caveat already documents as a known limitation. Since S1 has no NIR
band, this NIR fix cannot correct the Sept over-estimate; that is a separate, still-open item
(tracked as "mask slopes steeper than ~6 degrees using the site DEM" in `docs/progress.md`).

**Monitoring page:** `frontend/app/sentriq/app.tsx`'s lake-area panel now shows ISRO/NRSC's
published South Lhonak figures (`src_072`) as a static reference block, Teesta-only, sourced and
captioned "not used to calibrate the water-mask thresholds" — thresholds were not tuned against
these numbers, consistent with `src_072`'s own note and the instruction not to fit to them. Static,
not part of `GeeLayers` (contract §5.8) — a locked historical reference for one already-over event,
not a live-fetched quantity, so no contract/schema change. Frontend visual tests
(`frontend/CLAUDE.md`) were not run this session for the same tool-access reason.

## 2026-09-27 — M3→M4 routed discharge manifest (contract 0.3.0)

**Status:** artifact shape implemented; real routing-section selection and extraction are blocked
pending approved site inputs/engineering method. `routed_discharge/timeseries.csv` uses `t_s,q_m3s`;
`routed_discharge.json` records site/scenario/M3 run IDs, units, section LineString+CRS, routing
method, provenance and artifact-validation status (`contracts/schemas/routed_discharge.schema.json`).
The M4 builder accepts this manifest and derives its inlet velocity from the CSV values; it never
accepts a manually typed representative discharge in this path. Controlled fixtures prove file
handoff and consumption only; no scientific routing is claimed. Production remains blocked until
each site's cross-section, coordinate reference, extraction variable/integration method, and time
alignment convention are approved and sourced.

## 2026-09-27 — Cascade trigger representation remains unresolved

**Status: CONTRACT DECISION REQUIRED.** The 2026-09-24 `two_stage_imposed` decision explicitly left
per-dam `Dam.trigger` pending agreement against the site-level `cascade.trigger` sketch in handoff
§3.1. The choice affects M2 site-config validation and `cascade.py`, M3's stage-1 routed-discharge
extraction and stage-2 source/forcing construction, M4 scenario/inlet selection, and contract
schemas. Required fields include trigger scope (site default vs per-dam override), upstream
dam/run/scenario reference, threshold value/unit/source/status, routed inflow series reference,
crossing/interpolation convention, and downstream hydrograph `t_offset_s` provenance. The
threshold itself also requires real source data; it is not supplied by this contract decision. No
trigger representation or threshold is selected here.

## 2026-09-27 — Contract 0.3.0 routed-discharge and direct-run identifiers

Added `routed_discharge.schema.json` and documented M3→M4 artifacts under §4.4. The flood-response
`method` enum now includes `delft3d_direct` and `sph_direct`, matching provenance methods already
allowed in 0.2.0, so registered solver artifacts can be represented without pretending they came
from an emulator. Direct solver responses must identify their source run and confidence remains
LOW until a separately validated emulator exists. No production query is enabled until a run is
registered and its site inputs are cleared.

## 2026-09-24 — Site config schema: YAML v1 vs contract §3.1 (PENDING team decision)

**Status:** RESOLVED 2026-09-25 by user instruction — see "Site config: YAML v1 canonical, contract
0.2.0" below for the decision and what changed. Module-owner sign-off (contract §9 step 2) is still
outstanding; this is a working resolution, not yet a frozen contract.

`backend/shared/site_config.py` validates the format actually used in `sites/template.yaml` and
`sites/teesta.yaml` (`schema_version: 1`), **not** `docs/handoff_contract.md` §3.1. The contract has not
been edited. Differences to resolve:

| Topic | sites/*.yaml (v1, what the loader accepts) | contract §3.1 |
|---|---|---|
| Version key | `schema_version: 1` | `contract_version: 0.1.0` |
| Site id / name | `site: {id, name, region, river}` | top-level `site_id`, `name`, `state` |
| CRS | `crs.utm_epsg` as a SourcedValue | `crs_epsg` plain int |
| Domain keys | `far_field` / `near_field` | `farfield` / `nearfield` |
| Extent | `bbox` (SourcedValue) | `bbox_lonlat` (SourcedValue) |
| Resolution | `grid_resolution` (SourcedValue, m) | `cell_size_m` plain setting ⚙️ |
| Inflow point | `inflow: {from, location}` per domain | not in §3.1 |
| Dam id | bare slug (`south_lhonak`) | `<site_id>__<slug>` (§1.7) |
| POI id / type key | `id`, `category` (village, dam, bridge, hospital) | `poi_id` `<site>__poi__<slug>`, `kind` (adds town, school, …) |
| Dam kinds | moraine_dammed_lake, embankment_dam, concrete_dam, landslide_dam | natural_moraine, natural_landslide, embankment, cfrd, concrete, barrage |
| Breach input names | `breach_inputs.water_volume_above_invert`, … (no unit suffix) | `water_volume_above_breach_invert_m3`, … (flat, suffixed) |
| Enum values | `O/P`, `H/M/L` | `overtopping/piping`, `high/medium/low` |
| Units | `m^3` | `m3` (§1.1 suffix style) |
| `source` | free-text citation | `src_NNN` ID from docs/data_sources.md |
| `status` | `sourced` \| `placeholder` | adds `assumed` |
| Missing in v1 | — | `dem`, `landcover`, `cascade`, `thresholds`, `simulation`, `recheck`, `demo_mode`, `emulator_inputs`, `crest_elevation_m`, `reservoir_storage_m3`, `lake_area_m2`, `volume_elevation`, `equations_applicable`, `imposed_ranges` |
| Events | full `events` objects in the site file | list of event ids; details in docs/events/ |

Also found:
- `sites/teesta.yaml` refers to `sites/_template.yaml`; the file is `sites/template.yaml`.
- The repo has an empty `contract/` folder; CLAUDE.md and the contract say `contracts/`.

**Decide:** either migrate the YAML to §3.1 (and update the loader), or amend §3.1 to v1 (bump `contract_version`).

## 2026-09-24 — ID naming scheme and onboarding job state machine (PROPOSAL, pending review)

**Status:** proposal — not yet in `docs/handoff_contract.md`. Part 1 changes §1.7 and Part 2 changes
§5.3 / §5.1, so adopting either needs module-owner agreement and a `contract_version` bump (§9).
⚙️ marks settings the team still has to choose; no numbers below are measured values.

### Part 1 — ID naming scheme

**Rule: `__` (double underscore) separates hierarchy levels; `_` joins words inside a level.**
Any ID can be split on `__` into its parents without looking anything up:
`teesta__s007__delft3d` → site `teesta`, scenario `s007`, model `delft3d`.

Why change §1.7: today `scenario_id = <site_id>_s<NNN>` cannot be parsed reliably. A site named
`foo_demo` gives `foo_demo_s001`, which also reads as demo scenario 1 of site `foo`. `dam_id` and
`poi_id` already use `__`; this extends the same rule to scenarios and runs.

| ID | Pattern | Example | Scope / uniqueness |
|---|---|---|---|
| slug (building block) | `^[a-z0-9]+(_[a-z0-9]+)*$` | `south_lhonak` | lowercase ASCII, no `__`, no leading/trailing `_` |
| `site_id` | `^[a-z][a-z0-9]*(_[a-z0-9]+)*$`, 3–32 chars | `teesta`, `rishiganga` | global; never reused, never renamed |
| `dam_id` | `<site_id>__<slug>` | `teesta__south_lhonak` | per site (unchanged) |
| `poi_id` | `<site_id>__poi__<slug>` | `teesta__poi__chungthang` | per site (unchanged) |
| `event_id` | `<place_slug>_<YYYY>` | `teesta_2023`, `chamoli_2021` | global (unchanged) |
| `scenario_id` — design | `<site_id>__s<NNN>` | `teesta__s007` | per site, 001–999, **never reused** |
| `scenario_id` — demo mode | `<site_id>__demo_s<NNN>` | `new_site__demo_s003` | per site |
| `scenario_id` — historical | `<site_id>__hist_<event_id>` | `rishiganga__hist_chamoli_2021` | full `event_id`, so the event is traceable |
| `scenario_id` — named extra | `<site_id>__n_<slug>` | `teesta__n_full_volume` | per site |
| `run_id` | `<scenario_id>__<model>`, model `delft3d` \| `sph` | `teesta__s007__delft3d` | one per scenario × model |
| `query_id` | `q_<YYYYMMDDTHHMMSSZ>_<6 hex>` | `q_20260924T101500Z_3fa9c1` | global (unchanged) |
| `job_id` | `job_<YYYYMMDDTHHMMSSZ>_<6 hex>` | `job_20260924T101500Z_b17e02` | global (unchanged) |

Supporting rules:
- **Timestamps in IDs** are the UTC creation time, to the second, with `Z`. The hex part comes from
  `secrets.token_hex(3)`. The registry enforces a unique `job_id` / `query_id` (it retries on the
  one-in-16-million clash).
- **`query_id` and `job_id` stay site-free.** The registry stores `site_id`, so no change is needed.
- **Scenario numbers are never reused.** A re-run after `outdated` continues the numbering (for
  example `s031` onward). The registry stores which library each scenario belongs to in a new
  integer column, `scenarios.library_version`. The emulator `manifest.json` records its
  `library_version` and `run_ids`.
- **Retrying a failed run keeps the same `run_id`.** The registry counts attempts in a new
  `runs.attempt` column. The failed attempt's folder moves to `runs/_failed/<run_id>__a<N>/` for
  provenance.
- **Onboarding picks `site_id` explicitly.** The UI suggests a slug from the site name, and the
  user can edit it. If the ID is taken, the server returns `409 site_id_taken`; it does not
  auto-suffix, so IDs stay predictable.
- **File names follow the IDs, so they carry the same `__`.** For example
  `runs/teesta__s007__delft3d/` and `breach/hydrographs/teesta__s007__south_lhonak.csv`.

Impact if adopted: §1.7 examples, §4.2 hydrograph file names, §4.3 / §4.6 examples, and every
`scenario_id` / `run_id` in the site YAMLs and fixtures change. Nothing has been generated with
the old pattern yet, so there is no data to migrate.

### Part 2 — Onboarding job state machine

#### 2.1 Two separate things: job stage vs site status

- **Job `stage`** describes one onboarding or re-run job. It moves forward through milestones and
  ends in `ready` or `failed`.
- **Site `status`** (`SiteSummary.status`) is what the site can do right now. It is *derived* from
  the site's jobs and re-checks, and is never set by hand.
- **`outdated` is a site status, not a job stage.** It applies to a site whose job already
  finished `ready`, and is set later by a re-check or a config change.

#### 2.2 Job stages

Each stage names the **last milestone reached**. The step running "now" follows from the stage,
so the UI needs one field only.

```
 queued ──► dem_ready ──► breach_ready ──► simulating (k/N) ──► training ──► validated ──► ready
   │            │              │                  │                 │            │
   └────────────┴──────────────┴──────────────────┴─────────────────┴────────────┴──► failed
```

| Stage | Entered when | Running now | `progress` |
|---|---|---|---|
| `queued` | `POST /sites` accepted and config valid | nothing (`started_at` null) → then M1 terrain (`started_at` set) | none |
| `dem_ready` | M1 outputs (§4.1) written and pass raster checks (CRS, grid, nodata) | M2 breach ranges, then scenario design | none |
| `breach_ready` | `breach_params.json` and `scenario_design.json` written | launching the first solver run | `0 / N runs` |
| `simulating` | first run launched | Delft3D runs, one at a time (rule 13 RAM budget) | `k / N runs`, with `k` = runs `postprocessed` |
| `training` | all runs finished, or retries exhausted, and ≥ `min_runs_for_training` ⚙️ succeeded | PCA + GP fit, then LOOCV | `j / N folds` (LOOCV) |
| `validated` | `validation/loocv.json` written | publishing the emulator (swapped in atomically) | none |
| `ready` | manifest published; the site answers queries with `gp_emulator` | — (terminal) | none |
| `failed` | any unrecoverable error, or the user cancels | — (terminal) | frozen at last value |

Details:
- **`N` counts Delft3D design runs only.** SPH runs are comparison-only and take hours on the GPU,
  so they go into a separate `campaign` job after `ready` and do not hold up onboarding.
- **A failed solver run is retried `max_run_retries` ⚙️ times and then skipped.** `JobStatus`
  gains `runs: {completed, failed, running, pending}` so the UI can show skipped runs honestly.
  If fewer than `min_runs_for_training` ⚙️ succeed, the job fails with `too_few_runs`.
- **Poor validation does not block `ready`.** Poor LOOCV grades flow into the confidence rule
  (`validation_skill: POOR`). Being honest about confidence is the gate, not a hidden threshold.
- **`eta_s` stays `null` until the first run finishes.** After that it is the mean wall time of
  finished runs × remaining runs. The UI shows "estimate available after the first run", never a
  guessed number.
- **Cancelling is a `failed` job with `error.code: cancelled`.** This keeps the stage list short.
  It needs a new endpoint, `POST /jobs/{job_id}/cancel`.
- **A failed job can resume from its last milestone** via a new endpoint,
  `POST /jobs/{job_id}/resume`. For example, a job that failed during `simulating` keeps its
  finished runs. `JobStatus` gains `failed_at_stage`.
- **Demo mode uses the same stages** with `demo_mode: true`, `demo_mode.n_scenarios` runs at
  `demo_mode.cell_size_m`, and `__demo_sNNN` scenario IDs. It ends in site status `demo_mode`.
- **A re-run job (`kind: rerun`)** reuses the terrain, so it starts at `dem_ready`. It then follows
  the same path. The old library keeps serving until the new one is published at `ready`.

#### 2.3 Site status (derived)

| Site status | Condition | What queries return |
|---|---|---|
| `onboarding` | No published library yet, and the job is not `failed` | Nothing before `breach_ready`. From `breach_ready`: `empirical_fallback`, confidence LOW, caveat `empirical_fallback` |
| `demo_mode` | Published library came from a demo-mode job | `gp_emulator`, confidence LOW (contract §2.3), caveat `demo_mode` |
| `ready` | Published full library and not outdated | `gp_emulator`, computed confidence |
| `outdated` | Published library, plus one of the reasons below | the existing library + caveat `library_outdated` + `flags.library_outdated: true` |
| `failed` | No published library, and the latest job is `failed` | `empirical_fallback` if breach ranges exist, otherwise `409 site_not_ready` |

`outdated` reasons go in `status_reason_key`, with details in a new `status_detail` object:

| `status_reason_key` | Trigger | `status_detail` |
|---|---|---|
| `outdated_lake_area_change` | `recheck.json` has `change_pct` ≥ `recheck.lake_area_change_threshold_pct` | `{change_pct, threshold_pct, checked_at}` |
| `outdated_config_changed` | A fact in `sites/<id>.yaml` used in training has changed | `{changed_fields: [...]}` |

For the second trigger, the manifest stores a hash of the config fields used in training.

A site that is still `ready` but whose `recheck.next_check_at` has passed stays `ready` with a
"re-check overdue" banner. The lake may well be unchanged, so it is not marked `outdated`.

#### 2.4 What the UI shows at each stage

Every screen shows the same checklist in the style of `docs/ideation.md` ("DEM loaded ✓, breach
ranges computed ✓, Delft3D run 3/30 running…"). Text comes from `ui_text.json` keys
`job_stage_<stage>`. The API sends seconds; the frontend formats times.

| Stage | Headline | Checklist / progress | Map and panels | Actions |
|---|---|---|---|---|
| `queued` (not started) | "Waiting for the worker" | all steps pending | site bbox only | Cancel |
| `queued` (started) | "Preparing terrain" | Terrain ⟳ | site bbox only | Cancel |
| `dem_ready` | "Computing breach ranges" | Terrain ✓ · Breach ⟳ | domain, centreline, POIs; DEM source + vertical datum | Cancel |
| `breach_ready` | "Breach ranges ready — starting physics runs" | Terrain ✓ · Breach ✓ · Runs 0/N | the above + breach-range table (method pair per parameter, placeholders flagged); **fallback queries enabled, labelled "LOW CONFIDENCE — physics runs pending"** | Cancel · Try a fallback query |
| `simulating` | "Delft3D run k of N" | … · Runs k/N bar · failed/skipped count · ETA or "estimate after first run" | run table (scenario, status, wall time, mass-balance error); fallback queries still available | Cancel |
| `training` | "Training emulator" | … · Runs ✓ (k of N used) · Training ⟳ (LOOCV j/N) | run table frozen | Cancel |
| `validated` | "Validation complete — publishing" | … · Validation ✓ | LOOCV grades per output (extent / depth / arrival / velocity) | — |
| `ready` | "Site ready" (or "Demo-mode site ready") | all ✓ | grades, `last_trained_at`, next re-check date | Open site · Run SPH comparison |
| `failed` | "Onboarding failed at `<failed_at_stage>`" | ✓ up to the failed step, ✗ on it | error message; finished outputs stay visible | Resume · Edit config and restart |
| site `outdated` | "Library outdated: `<reason>`" (banner on every view of that site) | — | `status_detail` (e.g. lake area +X % vs threshold) | Re-run library |

Every stage also shows `has_placeholders` / `placeholder_fields` as a warning chip whenever they
are set (contract §0.5).

#### 2.5 Differences from the current contract

| Contract today | Proposed |
|---|---|
| §5.3 stages `queued → terrain → breach → design → simulating → training → validating → ready \| failed` (work in progress) | milestones `queued → dem_ready → breach_ready → simulating → training → validated → ready \| failed` |
| no cancel or resume | `POST /jobs/{id}/cancel`, `POST /jobs/{id}/resume`; `JobStatus.failed_at_stage`, `JobStatus.runs{…}` |
| `rerun` job kind has no stages | reuses the onboarding stages, starting at `dem_ready` |
| `SiteSummary.status_reason_key` only | adds `status_detail`; `outdated` gets two defined triggers |
| registry `scenarios`, `runs` | adds `scenarios.library_version`, `runs.attempt` |

### Open questions for the reviewer

1. Adopt the `__` separator for `scenario_id` / `run_id` (Part 1), or keep §1.7 as written and
   instead forbid site IDs containing `_demo`, `_hist` or `_s<digits>`?
2. Values for `min_runs_for_training` ⚙️ and `max_run_retries` ⚙️. Suggest setting them after the
   pilot gate, once real run times are known.
3. Confidence for an `outdated` site: keep the computed level plus a critical caveat, or cap it
   (for example at LOW)?
4. Should a lapsed re-check date make a site `outdated`, or only show the "re-check overdue"
   banner (as proposed)?
5. Accept the two new job endpoints (cancel, resume)?

## 2026-09-24 — M2 breach engine: XZ9/h_r blocker, Z20 dam-type mapping, breach_params.json additions (historical; XZ9 width blocker superseded)

**Historical status:** the XZ9 blocker described below was removed after the fixed Xu & Zhang
reference height was provided as 15.0 m. XZ9 breach width is implemented in
`backend/m2_breach/xz9.py`; the peak-discharge path remains unavailable. The Z20 mapping block
and contract additions described here remain in effect.

### XZ9 blocked (`backend/m2_breach/xz9.py`)

The initial implementation blocked both XZ9 outputs because `h_r` was undocumented in the
equation transcription. The fixed model constant is now `h_r = 15.0 m` per Xu & Zhang (2009).
The breach-width equation is active; peak discharge remains unavailable independently of this
reference-height decision.

**Consequence for the recommended method pairs** (`docs/paper_azmi.md`): XZ9 feeds the updated
DFM for Q_p and B_ave, and DFM 2024 for Q_p (`Q_p(DFM2024) = 1.23·F16 − 0.84·H14 + 0.26·XZ9`). So:

| Output | Recommended pair | Status today |
|---|---|---|
| Q_p | DFM_updated + DFM_2024 | **blocked** (peak-discharge XZ9 remains unavailable) — F16, Z20 still reported individually |
| B_ave | DFM_updated + XZ9 | computable when required physical inputs are present |
| T_f | DFM_updated + F8 | **computable** — neither needs XZ9 |

The former decision only allowed T_f a range; breach width now uses the documented fixed model
reference height and its existing recommended pair.

### Z20 blocked outside HD/CD (`backend/m2_breach/z20.py`)

`docs/Equations.md` §1.3 gives only HD and CD branches for Zhong et al. (2020); FD/ZD mapping is
NOT STATED, and §7 says to raise for any other dam type. Teesta III (`sites/teesta.yaml`) is FD,
so its Q_p output has Z20 blocked with warning `z20_dam_type_unmapped` — F16 is still reported.

### `breach_params.json` additions (agreed with user; additive, not a change to existing fields)

Contract §4.2's example shows every method as `{value, in_valid_range}`. A blocked method (or a
blocked dual-method range) instead writes `{value: null, status: "blocked", reason: "<why>"}`
with `low`/`high`/`in_valid_range` also null on a blocked range. `contracts/schemas/
breach_params.schema.json` (new) encodes both shapes; `contracts/examples/
breach_params.example.json` (new, generated from `sites/teesta.yaml`) shows a real blocked case.
`in_valid_range` is `null` everywhere in practice — `docs/Equations.md` states "Valid range: NOT
AVAILABLE" for every base equation, so no calibration-range check is invented.

Also: the §4.2 example has no top-level `caveats` field, only per-dam `warnings`. This
implementation follows the example as given — `moraine_extrapolation`, `placeholder_data`,
`failure_time_uncertain`, `clear_water` etc. are folded into `warnings`, not a separate list, to
avoid adding an unshown field.

**Decide:** whether to fold this shape back into `docs/handoff_contract.md` §4.2 once XZ9/Z20 are
unblocked, or keep it as a permanent "blocked equation" convention other modules may need too.

## 2026-09-24 — M2 breach hydrographs: coefficients, storage curve, Q_p-range check (DECIDED with user this session)

**Status:** implemented in `backend/m2_breach/{weir,storage,hydrograph}.py`. Contract §4.2's
hydrograph part (`hydrograph(site_id, dam_id, params)`, `hydrographs/*.csv` + sidecar) had not
been built yet (`docs/progress.md`, "out of scope this session, left for next").

### No built-in weir/side-slope/storage coefficients (CLAUDE.md rule 3)

`backend/m2_breach/weir.py`'s broad-crested weir equation and `storage.py`'s area-volume
relation are standard hydraulics, **not** from `docs/Equations.md` (that document's scope is the
Azmi 2026 breach-parameter equations). No source in this repo gives a weir coefficient, breach
side slope or area-volume exponent, so the code has **no default value** for any of them —
inventing one would violate rule 3. They are additive, optional fields on `Dam`
(`backend/shared/site_config.py`): `volume_elevation` (mirrors contract §3.1's block already
drafted there but missing from the v1 loader — see the pending site-config-schema decision above)
and a new `breach_hydrograph` block (`weir_coefficient_rect`, `weir_coefficient_side`,
`side_slope_z`, all SourcedValues). A `Dam` without both blocks fully sourced cannot use
`breach_growth_weir`; `hydrograph_for_dam` falls back to the volume-conserving `triangular`
method (needs `params["peak_discharge_m3s"]`), or raises `HydrographBlocked` if neither is
available — the same "block, don't guess" pattern as XZ9.

### Storage curve when bathymetry is missing

`storage.from_area_volume_relation(V_w, h_w, b)` derives `V(h) = V_w * (h/h_w)^(b/(b-1))` from
the site's area-volume exponent `b` (`V = a*A^b`) and the one calibration point M2 already has —
this is an SIH26-derived shape assumption (self-similar basin), not a published formula; every
hydrograph built from it carries caveat `storage_from_area_volume_relation`. When bathymetry
exists, `storage.from_surveyed_curve(points, invert_elevation_m)` interpolates it directly with
no caveat.

### `peak_within_m2_range` when the Q_p range is blocked

The M2 Q_p range (`DFM_updated`+`DFM_2024`) remains blocked while the XZ9 peak-discharge path is
unavailable, so the synthetic test dam's weir-routed peak (~1500 m³/s at
V_w=1e6, B_ave=40m) can't be checked against it. Agreed: `peak_within_m2_range` is `null` (not
`False`) with caveat `m2_qp_range_blocked` when the range is blocked, and tests check the flag is
correctly `null`/`true`/`false` rather than picking inputs to force `true`.

### Breach growth: width and invert depth together

Per Fread/HEC-RAS-style breach growth, the invert drops linearly from the initial water surface
to the final invert over `failure_time_s` while the bottom width grows from 0 to
`B_ave - z*h_b` (B_ave being the mean of top and bottom width, `docs/Equations.md` §0). This keeps
`Q=0` at breach start for any side slope `z`, avoiding the discontinuity a width-only growth model
would have.

### New contract files

`contracts/schemas/hydrograph_sidecar.schema.json` (new) encodes contract §4.2's sidecar shape
plus additive fields `peak_within_m2_range`, `has_placeholders`, `caveats`, `provenance` (same
pattern as the `breach_params.json` additions above). `contracts/examples/
hydrograph_sidecar.example.json` (new) is generated by actually running `breach_growth_weir()`,
not hand-written.

## 2026-09-24 — M2 cascade engine: two-stage imposed hydrograph (DECIDED with user this session)

**Status:** implemented in `backend/m2_breach/cascade.py`, `backend/shared/site_config.py`. This
finally resolves `docs/handoff_contract.md` §3.1's `cascade.approach: null ⚙️` for the case M2
needs today (Teesta: South Lhonak GLOF → Teesta III), though the contract doc itself is not
edited — see "Site-config schema" decision at the top of this file for why the v1 loader keeps
diverging from §3.1 until that migration happens.

### Approach chosen: `two_stage_imposed`, not `dambreak_structure`

Confirmed with the user (both options were live in §3.1 and in `docs/ideation.md`'s "model its
failure as an imposed scenario" note): M2 does **not** route the flood itself. A downstream dam's
own breach hydrograph is triggered once **routed inflow** at that dam — an input time series
produced by the stage-1 Delft3D run's observation cross-section (M3), or M5's HAND fallback —
first reaches a threshold. `backend/m2_breach/cascade.py`'s `cascade_plan()` raises
`UnsupportedCascadeApproach` if it sees `cascade.approach: dambreak_structure`; that approach
(breaching modelled dynamically inside a Delft3D structure) belongs to M3, and is out of scope
for this module. No celerity/attenuation value is invented to do the routing in M2 (CLAUDE.md
rule 3) — see `trigger_time()`'s docstring.

### Per-dam trigger threshold, not the single site-level `cascade.trigger` in §3.1 (PENDING team agreement)

Contract §3.1 sketches one site-level `cascade.trigger.value_m3s`. With a chain of dams (or more
sites onboarded later), different dams have different capacities, so this deviates: `Dam.trigger`
(`{type: inflow_threshold, value: SourcedValue m^3/s}`) is per dam, required on every dam with
`triggered_by` set when `cascade.approach == two_stage_imposed`
(`SiteConfig._cross_checks`). **This is additive and PENDING team agreement** — the contract's
single site-level `trigger` block is not removed from the doc, and could still be adopted as a
site-wide default with per-dam overrides if the team prefers. `sites/teesta.yaml`'s
`teesta_iii.trigger.value` is `status: placeholder` — not sourced yet (needs the spillway/outlet
capacity or the 2023 failure timeline).

### Superposition, not routed-inflow-through-storage

The triggered dam's hydrograph (`cascade.triggered_hydrograph`) releases only **its own** stored
volume (`hydrograph_for_dam`, unchanged) — it does not level-pool route the incoming upstream
flood through the reservoir during the breach. The routed upstream flood keeps flowing through
the stage-2 hydraulic model and is added there (superposition), not by M2. This was the simpler
of two options discussed; the alternative (inflow term in the storage ODE) would need M3 to know
not to also inject the upstream flood at that point, adding coupling for a physical effect
(reservoir filling before breach) that is already a known limitation. Caveat
`cascade_superposition` records this on every triggered hydrograph.

### `equations_applicable` / `imposed_ranges` now enforced by the loader, not `compute_dam`

`docs/Equations.md` §7 says to refuse `kind: concrete_dam`. Previously `breach_params.compute_dam`
raised `DamKindRefused` for it outright — but §4.2 says such a dam should report `imposed_ranges`
from the config instead, flagged `concrete_dam_imposed` (this was simply not built yet). Now:

- `SiteConfig._cross_checks` requires `equations_applicable: false` whenever `kind: concrete_dam`,
  and requires `imposed_ranges` whenever `equations_applicable: false`. `DamKindRefused` is
  deleted; a bad config is now a loader error instead of a `compute_dam` exception.
- `compute_dam` for `equations_applicable: false` builds each output range straight from
  `imposed_ranges` (`interval: "imposed"`, `selected_pair: null`, `source` from the config's
  `SourcedValue`), or `status: "blocked"` if that range is itself a placeholder — same
  "block, don't guess" pattern as XZ9.
- `breach_params.schema.json`'s `OutputRange.interval` gains `"imposed"`; `selected_pair` may be
  `null` for it. `hydrograph_sidecar.schema.json` gains an optional `trigger` object.

`sites/teesta.yaml`'s `teesta_iii` stays `kind: embankment_dam` / `dam_type: FD` — its true dam
type is still unverified (`docs/ideation.md`: "Verify Teesta III dam type before applying
embankment equations"). If it turns out to be concrete, flip `equations_applicable: false` and
fill `imposed_ranges` from a source; no code change needed.

### Tests

`tests/fixtures/m2_breach/synth_cascade.yaml` (new): a synthetic two-dam site (`synth_lake` →
`synth_dam2`, concrete, imposed ranges, per-dam trigger), fully sourced so cascade tests aren't
tangled with placeholder-detection tests. Loader cross-checks are tested in
`tests/shared/test_site_config.py`; `cascade_plan`/`trigger_time`/`triggered_hydrograph` (including
a three-dam chain, an end-to-end lagged-hydrograph scenario, and the placeholder-threshold block)
are tested in `tests/m2_breach/test_cascade.py`.

## 2026-09-25 — M5 LOOCV: additive validation-report fields (NEEDS TEAM SIGN-OFF)

**Status:** implemented, additive-only, not yet confirmed by the team.

`docs/m5_specs.md` §8 (acceptance test A1) requires the GP be checked against **two** baselines
("Linear-in-scores" and "Nearest-run blending"), but `docs/handoff_contract.md` §4.6's
`validation/loocv.json` shape only has one baseline key, `baseline_linear`. Per user instruction
this session, resolved by keeping `baseline_linear` exactly as contracted (linear-in-scores) and
adding a sibling `baseline_nearest` key with the same shape (`extent.iou_median`,
`depth.rmse_wet_m_median`, `arrival.mae_s_median`), extended with `extent.f1_0_3_median` and
`arrival.rmse_s_median` since A1/A4 need those too. `contracts/schemas/validation.schema.json`
does not `additionalProperties: false` on the top level, so this validates without a schema change.
Also additive, for the same reason (not in the contract's `loocv.json` sketch, needed for an honest
A1-A8 report): `per_run[].extra` (arrival RMSE, POI/by-output coverage, terrace flag, PCA
projection RMSE, both baselines' per-run metrics), `acceptance` (the A1-A8 table),
`settings`/`caveats`/`provenance`/`notes`.

Grades: spec §6's skill-check cut-offs are only defined for extent (F1 >= 0.85/0.70) and arrival
(RMSE <= 10%/20% of mean true arrival). Depth and velocity have no cut-off in the spec, so their
`summary.*.grade` stays `"UNKNOWN"` rather than inventing thresholds — flagged for the team to set
in a future session (`backend/m5_emulator/loocv.py`'s `GradeThresholds`).

The synthetic-world report (`reports/m5_synthetic/validation/loocv.json`, gitignored, **not**
under `data/`) uses `model: "synthetic"`, outside the contract's `delft3d | sph` enum — same
reasoning as `library.py`'s existing "never write synthetic run_ids into data/". The CLI validates
the report against `validation.schema.json` with `model` substituted to `"delft3d"` for the check
only, and records this as a `notes` entry in the report itself so it's never silently passed off as
real.

## 2026-09-25 — M6 loss estimation: JRC depth-damage functions, additive asset_values.csv columns

**Status:** implemented, additive-only.

`loss_inr` (`docs/handoff_contract.md` §4.7) is priced from the JRC global flood depth-damage
functions (Huizinga et al. 2017, `docs/data_sources.md` src_031/src_032) — the ASIA continent
damage curves and India's max-damage values, both extracted from the published workbook by
`backend/m6_impact/jrc_damage.py`. Full rationale for every default in
`docs/impact_outputs.md` §5.

Per user instruction this session: `asset_values.csv` gains two additive columns beyond the
contract's `asset_class,value_inr_per_unit,unit,source,status` — `value_eur2010` (the JRC
figure before currency conversion) and `jrc_cell` (the exact sheet/cell it came from), so every
`value_inr_per_unit` is re-derivable and auditable rather than an opaque number. This is a
contract change (`docs/handoff_contract.md` §4.7 updated in the same commit); flagged per
CLAUDE.md rule 1 rather than applied silently.

JRC values are 2010 EUR; the contract wants INR at current prices. Per user instruction, the
conversion is two factors in `config/impact.yaml`, applied in order — EUR→INR at the 2010
annual-average RBI reference rate, then a 2010→current Indian price index (CPWD cost index or
WPI, team to choose which) — both currently `status: placeholder`. Until both are sourced,
`loss_inr` and every `by_asset_class` entry are null Estimates (never an invented FX rate or
index, CLAUDE.md rule 3); `has_placeholders: true` propagates honestly. The same applies to
`config.loss.default_road_width_m` (no source yet for road width by class), which keeps roads
unpriced independently of the FX/index state.

Scope limit, not yet resolved: `loss_inr` prices only buildings (`buildings.gpkg`) and roads
(`roads.gpkg`). Hospitals, schools and bridges (`facilities.gpkg`) are points with no footprint
polygon, so there's no area to apply a JRC per-m² damage value to without inventing one;
agriculture has no cropland exposure layer yet. Both are named in every result's `assumptions`
rather than silently omitted or estimated with a guessed area.

## 2026-09-25 — Site config: YAML v1 canonical, contract 0.2.0

**Status:** decided by user instruction this session; module-owner sign-off (contract §9 step 2)
still outstanding. Resolves the 2026-09-24 "Site config schema: YAML v1 vs contract §3.1" entry
above — read that entry's diff table first for exactly what changed.

**Decision:** `sites/*.yaml` and `backend/shared/site_config.py` stay as they are (schema_version 1);
`docs/handoff_contract.md` §3 is rewritten to mirror them, and `contract_version` bumps 0.1.0 →
0.2.0 (before-1.0 breaking change, contract §9). Concretely:

- Contract §1.3/§1.7, §3 (all of §3.1/§3.1a/§3.2/§3.3), §4.1, §4.2, §5.1, §5.2 rewritten; every
  `"contract_version"` in the doc and in `contracts/examples/*.json` + `styles.json` bumped to
  0.2.0; `CONTRACT_VERSION` constants in `backend/{shared/grid,m2_breach/*,m5_emulator/*}.py` and
  the FastAPI app version in `backend/m0_api/main.py` bumped to match.
- **IDs are bare local slugs inside the YAML** (`south_lhonak`), and the global `dam_id`/`poi_id`
  (§1.7) is *derived* by prefixing the site id (`teesta__south_lhonak`) — not stored in the YAML
  itself. This documents what the loader already does; it does **not** fix two known bugs found
  while writing this: `backend/m2_breach` writes the bare slug as `dam_id` in
  `breach_params.json`/hydrograph sidecars (should be derived), and `backend/m5_emulator` builds
  `poi_id` from a POI's display `name` instead of its `id`. Both are flagged for their own M2/M5
  sessions, not fixed here (CLAUDE.md "one module per session").
- The separate 2026-09-24 "ID naming scheme" proposal (fully `__`-delimited `scenario_id`/`run_id`)
  is **not** adopted by this decision and stays pending on its own.
- **New optional `Dam` field: `initial_water_level`** (m, `LengthValue`) — was in the 0.1.0 draft
  (`initial_water_level_m`, §3.3) but had no home in the implemented schema. Added to
  `backend/shared/site_config.py`, `sites/template.yaml` (commented, optional) and
  `sites/teesta.yaml` (`teesta_iii`, `status: placeholder` — no invented reservoir level, CLAUDE.md
  rule 3).
- **`sites/teesta.yaml` additions**, both null placeholders, no invented numbers: `equations_applicable: true`
  made explicit on both dams (was relying on the pydantic default; for `teesta_iii`, explicit because
  its `kind` is `embankment_dam`, believed concrete-faced rockfill — Xu & Zhang dam-type `FD`, which
  the equations do cover — not `kind: concrete_dam`, which the loader blocks per `docs/Equations.md`
  §7); `volume_elevation` (method `area_volume_relation`, `area_volume_exponent_b` placeholder) on
  both dams, needed for `breach_growth_weir` — without it `hydrograph.py` keeps falling back to
  `triangular`, unchanged behaviour, just now an explicit documented gap instead of a silent one.
- **`crs.utm_epsg.status` → `sourced`.** The value (EPSG:32645) was already correct; the field was
  `placeholder` only because no one had written down that it's a *derivation*, not a lookup: UTM
  zone 45N covers 84°E–90°E, the far-field bbox (88.10–88.85°E) lies entirely inside it, so the EPSG
  code follows from the bbox alone via the EPSG registry, with no DEM-choice dependency (contract
  §1.3's "confirm against the DEM projection" was itself a misconception — we reproject onto this
  CRS on load regardless of the DEM's native projection).
- `contracts/schemas/site_config.schema.json` added — the one schema in `contracts/` generated
  directly from `SiteConfig.model_json_schema()` rather than transcribed from the contract prose
  (`contracts/README.md` explains why). `site_create_request.schema.json`'s `site_config` field now
  `$ref`s it; found and fixed a real bug while wiring this up: `backend/m0_api/main.py`'s
  `create_site` read `site_config.get("site_id")` (the 0.1.0 draft's flat key, never valid), now
  reads `site_config["site"]["id"]`. Test payloads in `tests/m0_api/test_endpoints.py` (previously
  `{"site_config": {"site_id": "kosi"}}`, never a valid config) now build a full config from
  `tests/fixtures/shared/synth.yaml` with `site.id` overridden.
- Fixed while here: `sites/teesta.yaml`'s header comment pointed at `sites/_template.yaml`; the file
  is `sites/template.yaml`.
- `docs/handoff_contract.md` also flags several 0.1.0-draft fields as **not implemented** and out of
  scope for this change: `dem.source`/`landcover.source` (deliberately deferred — chosen from the
  M1-2 DEM comparison report, not pre-selected in config), `thresholds`, `simulation`, `recheck`,
  `demo_mode`, `emulator_inputs` (§3.3 stays documentation-only until M2/M5 wire it up),
  `crest_elevation_m`, `reservoir_storage_m3`, `lake_area_m2`. Adding any of these later is an
  additive contract change (§9), not a silent YAML addition.

## 2026-09-25 — M1 terrain pipeline: settings, Manning table path, water extent, flow routing

Decided with the user while building `backend/m1_terrain` end to end (dem/water/burn/hydro/
centreline/roughness/domain/stl/pipeline). `docs/handoff_contract.md` §4.1 updated to match
(additive — no `contract_version` bump: the section was still unimplemented, nothing downstream
is pinned to the old text, and the doc header already marks the contract DRAFT).

- **Manning table path/columns.** The contract said `data/manning_table.csv` with
  `class_code,class_name,manning_n,manning_n_low,manning_n_high,source`. The table that actually
  exists is `config/manning_n.csv` (`worldcover_code,class,n_default,n_min,n_max,source,
  source_row,confidence,status,notes`), committed to git this session. Decision: keep it at
  `config/` (it's project-maintained, not a raw download — `data/` is gitignored per §1.8) and
  update the contract's path/columns rather than reshaping the file or generating a second copy.
  Every row is `status: placeholder` (Chow 1959 proxies — see the file's own `notes` column), so
  every `roughness.tif` this pipeline produces sets `has_placeholders: true`.
- **Lake/reservoir extent.** Neither the site config nor M7 (not built yet) provide a lake/
  reservoir polygon. Decision: derive it from ESA WorldCover's permanent-water class (80),
  restricted to the connected component within `TerrainSettings.snap_radius_m` (default 300 m) of
  each dam's `location` — a moraine-dammed lake or a dam's reservoir both sit immediately against
  the dam point by construction, so this needs no flow-direction pass. An optional vector polygon
  (for M7's future `lake_latest.geojson`) overrides WorldCover entirely when supplied. New output
  `water_mask.tif` (uint8: 0 land, 1 lake, 2 reservoir) records the result, added to contract §4.1.
- **Flow routing: no richdem.** `richdem` is in CLAUDE.md's Stack list but has no wheel for this
  environment's Python 3.12 + numpy 2.5, and building it from source wasn't worth the setup cost
  for this session. `backend/m1_terrain/hydro.py` implements priority-flood fill (Barnes, Lehman &
  Mulla 2014, epsilon variant) + an implicit D8 drainage tree + flow accumulation in plain numpy/
  heapq — no new dependency. `docs/paper_*.md` don't cover this (it's terrain preprocessing, not
  the breach/emulator methods those papers describe), so it isn't a CLAUDE.md rule 4 equation.
- **Domain height-above-channel default: `TerrainSettings.domain_max_hand_m = 50` (m).** Generous
  enough for GLOF flows tens of metres deep while still bounding the Delft3D mesh size; overridable
  per run (`--domain-max-hand-m`). Every other M1 setting (snap radius, crest search distance,
  etc.) lives in `backend/m1_terrain/settings.py` with its default and reasoning in the module
  docstring, not invented ad hoc in `pipeline.py`.
- **DEM product: no default.** `backend.m1_terrain.pipeline`'s `--dem` argument is required. The
  M1-2 DEM comparison report (void %, difference maps, valley profiles — still to come) is what
  should pick the product; this session's pipeline only consumes that choice, recorded verbatim in
  `terrain/provenance.json`.
- **Near-field frame origin: the near-field grid's lower-left corner** (not the dam or an
  arbitrary point), so every mesh vertex has local x, y >= 0. Contract §4.1's `nearfield_frame.json`
  row updated to say so explicitly.
- **Reservoir bathymetry is unavailable from a surface DEM.** `burn.burn_reservoir` flattens a
  reservoir's WorldCover footprint to a single water-surface elevation (median DEM value over the
  footprint) rather than inventing a bed. Always emits caveat `placeholder_data` when it fires
  (CLAUDE.md rule 3 — this is exactly the "missing fact, don't guess" case, at raster granularity
  rather than a single `SourcedValue`).

## 2026-09-25 — M7 GEE fetch: lake-area series, rainfall, cache, recheck

Decided with the user while building `backend/m7_gee/{settings,lake_area,provider,rainfall,
recheck,cache,fetch}.py` end to end (contract §4.8: `lake_area.csv`, `lake_latest.geojson`,
`rainfall.csv`, `gee_meta.json`, `recheck.json`). Out of scope this session: event imagery PNG/TIF,
`observed/*.geojson` (manual digitising) and wiring the real M0 `/gee` endpoints to this cache
(`backend/m0_api/main.py`'s `GET/POST /gee/{site_id}` still serve `gee_layers.example.json`).

- **Water classification: Otsu's threshold per monthly composite** (`lake_area.otsu_threshold`),
  not one fixed NDWI/backscatter cutoff. A single global cutoff would have to be re-justified for
  every site and season (turbid glacial water, different SAR incidence angles); Otsu adapts and
  the threshold actually used is recorded per month in `gee_meta.json`'s `lake_area_months`. The
  result is clamped to `GeeSettings.ndwi_threshold_clamp` / `s1_vv_threshold_clamp_db` so a
  degenerate histogram (e.g. an almost-uniform composite) can't pick a physically nonsensical
  threshold. Otsu's tie-breaking: when two classes are separated by empty histogram bins, every
  threshold in the gap gives the same between-class variance; `otsu_threshold` picks the middle of
  the tied run, not its first (leftmost) index, so the threshold sits in the gap rather than at
  its edge.
- **Only the lake's connected component counts** (`lake_area.seed_component`), found by seeding a
  connected-components labelling at the dam's `location` (or the nearest labelled pixel, if the
  seed itself isn't classified as water that month) and keeping only that component. A global
  threshold over the AOI buffer also lights up unrelated water and, for Sentinel-1, radar shadow;
  seeding on the known lake location drops both without a fixed distance cutoff.
- **Frozen months are skipped, not written with a falsely small area.** Both NDWI and SAR
  under-detect an ice-covered lake. `lake_area.choose_method` skips a month outright (no
  `lake_area.csv` row's `area_m2` — cloud/valid data still recorded) once the Sentinel-2 SCL
  snow/ice class covers more than `GeeSettings.max_snow_ice_pct` (default 30%) of the AOI buffer,
  checked before the cloud/S1-fallback decision since ice degrades both products the same way. The
  reason (`snow_ice` / `no_usable_scene`) is recorded per month.
- **Method choice order: Sentinel-2 NDWI first, Sentinel-1 VV fallback, skip last.** S2 is used
  when its cloud share over the buffer is at or below `GeeSettings.max_cloud_pct` (default 20%);
  otherwise S1 is tried, used only if its pixel coverage is at least `GeeSettings.min_valid_pct`
  (default 50%) — a month can otherwise fall in a gap in Sentinel-1's revisit schedule.
- **Rainfall catchment: HydroBASINS level 12** (`WWF/HydroSHEDS/v1/Basins/hybas_12`), the basin
  containing the lake plus every basin upstream of it (walked via `NEXT_DOWN`,
  `provider.walk_upstream_basin_ids`, capped at `MAX_UPSTREAM_HOPS = 25` hops). No basin/catchment
  polygon exists anywhere else in the repo (M1 has no basin output, and the far-field bbox likely
  cuts off a GLOF lake's upstream glaciers), and HydroBASINS needs no M1 outputs to exist first, so
  the recheck doesn't depend on the emulator having been trained. `rainfall.csv`'s `aggregation` is
  `catchment_mean_daily_total`.
- **`recheck.json`'s `reference_area_m2`** is the latest `lake_area.csv` row dated on or before the
  trained emulator's `manifest.json` `trained_at` (`backend/m5_emulator/emulator.py`) —
  `recheck.reference_area`. This compares against what the emulator actually saw, not an arbitrary
  baseline. With no trained library yet (no `manifest.json`), `recheck.json` reports
  `reference_area_m2: null`, `outdated: false`, `reason: "no_trained_library"` — an untrained site
  is never flagged outdated. `--reference-area-m2` overrides the lookup.
- **`recheck.json`'s `threshold_pct` default: 10%** (`GeeSettings.recheck_threshold_pct`), matching
  the value already in `gee_layers.example.json` (§5.8). **PENDING team agreement** — the site
  config has no `recheck` block (`docs/handoff_contract.md` §3.1's "Missing in v1" list), so this
  is a function argument / `GeeSettings` default for now, same as M1's settings pattern
  (`backend/m1_terrain/settings.py`), not a config field. `PUT /sites/{id}/recheck`'s
  `lake_area_change_threshold_pct` (`recheck_request.schema.json`) will override it once M0
  persists that per-site (not wired up yet — `set_recheck` in `backend/m0_api/main.py` is still a
  mock that never reads its own request body's threshold back out).
- **Caching: past finished months are never refetched**, only the two most recent
  (`fetch.REFETCH_TRAILING_MONTHS = 2`) plus any month not already on disk
  (`cache.merge_lake_rows`). `lake_latest.geojson` is only rewritten when the latest valid month
  was actually refetched this run (so its raster component is in memory); otherwise the existing
  file is left alone rather than silently going stale in a way that looks fresh.
- **Provider failure keeps the cache.** `fetch.run` catches a failing lake-area or rainfall fetch
  separately, logs it, returns it in `FetchResult.errors` (never swallowed), and writes
  `gee_meta.json` `source: "cache"` for that product instead of failing the whole run — matches
  contract §4.8 `gee_meta.json`'s documented fallback order (live → cache → screenshot_fallback).
- **`gee_layers.schema.json`'s `recheck.change_pct` now allows `null`** (was `type: number`),
  additive/non-breaking (contract §9: still-draft section, no existing consumer reads it as
  non-nullable — `gee_layers.example.json`'s literal payload is unaffected). Needed for
  `cache.load_layers()` (built for M0 to use later, not wired up this session) to represent the
  untrained-library case honestly instead of inventing a 0.
- **New CHIRPS/Sentinel-1/Sentinel-2/HydroBASINS `data_sources.md` entries** (`src_038`–`src_041`),
  same "verify DOI before `status: sourced`" caveat as `src_033`/`src_034` — this session cites the
  EE Data Catalog collection IDs (verified against the catalog directly) but not a peer-reviewed
  citation for each dataset.
- **`EarthEngineProvider` (the real Earth Engine calls) is not exercised by `pytest`** beyond
  request-shape checks (`tests/m7_gee/test_provider.py`: `_pixel_grid`,
  `walk_upstream_basin_ids`). The classification/orchestration logic (`lake_area.py`, `rainfall.py`,
  `recheck.py`, `fetch.py`) runs end to end against `provider.SyntheticProvider`
  (`tests/m7_gee/test_fetch.py`), matching CLAUDE.md rule 2. `EarthEngineProvider` itself needs a
  live smoke test with real EE credentials: `python -m backend.m7_gee.fetch teesta --months 24
  --ee-project <proj>`, not yet run this session.

## 2026-09-25 — M7: recording scene IDs, observed-extent loader

Two "out of scope" leftovers from the previous M7 sessions, both decided with the user this
session.

- **Satellite scene IDs are recorded in `gee_meta.json`, not `sites/<site_id>.yaml`.**
  `Event.imagery_pre_event`/`imagery_post_event` (`site_config.py`, `contracts/schemas/
  site_config.schema.json`) only carry `value` (a date) + `source` (a file path, parsed by
  `imagery.raw_rgb_path`) + `status`, and the schema has `additionalProperties: false` — adding a
  `scene_id` field there is a contract change (CLAUDE.md rule 1: never change the contract
  silently). Asked the user; decided to put scene IDs where contract §4.8 already designed for
  them: `gee_meta.json`'s "per product: dataset, scene_ids, acquisition_dates, cloud_pct,
  fetched_at, source" — `imagery.py` had never actually written an `"imagery"` entry there
  (only `lake_area`/`lake_latest`/`rainfall` were). `imagery.convert()` now takes an optional
  `scene_ids={"pre": [...], "post": [...]}` and merges an `"imagery"` entry into `gee_meta.json`
  without touching the other products' entries; `sites/teesta.yaml` only got its `imagery_pre_event/
  post_event.value` dates corrected to match the actual scene acquisition dates
  (`2023-09-26`/`2023-10-26`, from `COPERNICUS/S2_SR_HARMONIZED/20230926T.../20231026T...`) — no
  schema change. CLI: `python -m backend.m7_gee.imagery <site_id> --pre-scene-id ID
  --post-scene-id ID` (repeatable).
- **New `backend/m7_gee/observed.py`**: stamps an operator-supplied GeoJSON (already EPSG:4326)
  with the contract's required properties (`event_id, method, imagery_ref, digitized_by, date,
  kind: observed`) and writes `data/<site_id>/gee/observed/<event_id>_observed.geojson`.
  `cache.read_observed_extents()` now reads every file in that directory into
  `GeeLayers.observed_extents` (was hardcoded `[]`) — wired and tested against a synthetic GeoJSON
  fixture, per the user's choice this session. **Not run for real**:
  `data/teesta/observed/flood_extent_2023.geojson` (the hand-digitized outline the user described)
  still doesn't exist on disk — same blocker noted in the previous M7 session. Once it exists:
  `python -m backend.m7_gee.observed teesta sikkim_glof_2023 data/teesta/observed/
  flood_extent_2023.geojson --digitized-by <name>`.
- **Test environment note**: this session's `pytest` runs used system `python3` (no `shapely`
  installed, unlike the project's `sih26` conda env) — `tests/m7_gee/test_fetch.py`,
  `test_lake_area.py`, `test_provider.py`, and all of `tests/m0_api` fail to collect for that
  reason alone (`lake_area.py` imports `shapely` at module level, and `m0_api.main` imports
  `fetch`). Unrelated to this session's changes; `tests/m7_gee/test_imagery.py`,
  `test_observed.py`, `test_cache.py` (the files touched this session) all pass. Full `pytest -q`
  in the real conda env not run this session — should be done before merging.

## 2026-09-25 — M0 scheduled site re-checks: persistence, outdated triggers, rerun scope (DECIDED with user this session)

**Status:** implemented in `backend/m0_api/site_status.py` and `backend/m0_api/worker.py`. Four
questions were open before writing any code; the user decided all four.

1. **Where site status/recheck state lives:** `data/<site_id>/site_status.json`, not a `sites`
   table. Registry §4.5 freezes the table list at `scenarios`/`runs`/`jobs`/`queries`; adding a
   table would be a contract change needing the §9 sign-off. A per-site JSON file needs none —
   same pattern as `manifest.json`/`recheck.json`/`gee_meta.json`. `status` itself
   (`onboarding`/`demo_mode`/`ready`/`failed`) is still meant to be derived from a site's jobs
   (§2.1 below), never stored; this file only carries what a re-check can add on top
   (`outdated`, `status_reason_key`, `status_detail`, the `recheck` schedule).
2. **Open question #4 (a lapsed re-check date alone) is resolved: banner only, stays `ready`.**
   A missed check-in doesn't mean the lake changed. `site_status.is_due()` makes a lapsed job
   *eligible to be queued*; it never by itself sets `outdated`.
3. **Library-age check:** age of the trained library (`manifest.json` `trained_at`) vs. a new
   engineering knob, `site_status.DEFAULT_MAX_LIBRARY_AGE_DAYS = 365` (not a physical fact,
   same class as `GeeSettings.recheck_threshold_pct`). Deliberately far above any sane
   `frequency_days` (default 90, contract §5.1's own example value) so a merely-overdue check can
   never look like this trigger — see point 2. New `status_reason_key`:
   `outdated_library_age` (site_summary.schema.json leaves `status_reason_key` an unconstrained
   string, so this needed no schema change; the existing `outdated_lake_area_change` and
   `outdated_config_changed` table below gains a row).
4. **`POST /sites/{id}/rerun` stays a stand-in, not fully real.** It now queues a genuine
   `onboarding`-kind job (not a `rerun`-kind one — `jobs.STAGES` still has no `rerun` entry,
   the open gap noted in `docs/progress.md`), so it does **not** actually skip terrain the way
   "re-run reuses existing terrain" implies. Deciding real `rerun` stages (Part 2 above proposes
   starting at a milestone after terrain, under stage names not yet adopted) is separate,
   out-of-scope work.

`outdated` reasons (§2.3 above), with `outdated_library_age` added by this session:

| `status_reason_key` | Trigger | `status_detail` |
|---|---|---|
| `outdated_lake_area_change` | `recheck.json` has `change_pct` ≥ `recheck.lake_area_change_threshold_pct` | `{change_pct, threshold_pct, checked_at}` |
| `outdated_config_changed` | A fact in `sites/<id>.yaml` used in training has changed | `{changed_fields: [...]}` |
| `outdated_library_age` | `now - manifest.trained_at > DEFAULT_MAX_LIBRARY_AGE_DAYS` | `{trained_at, age_days, max_age_days}` |

The worker schedules a `recheck` job (real `checking` stage, not the fake sleep every other
in-process stage still uses) for any known site with a published library and no active job once
its `site_status.json` schedule is due — no separate timer/cron, just a cheap check every
`tick()`. The `checking` stage runs M7's lake-area check
(`gee_fetch.best_effort_provider()` + `gee_fetch.run()`, extracted from what
`POST /gee/{id}/refresh` already did inline, now shared) then the library-age check, and calls
`site_status.record_check(...)`. `GET /sites`/`GET /sites/{id}` overlay this state onto the
still-mocked base response; `PUT /sites/{id}/recheck` persists for real.

## 2026-09-25 — M3: Delft3D 4 FLOW, not FM (DECIDED with user this session)

> **SUPERSEDED 2026-09-26** by "M3: ANUGA replaces Delft3D 4 FLOW" (end of this file), itself
> superseded the same day by "M3: back to Delft3D FM, superseding 'ANUGA replaces Delft3D 4
> FLOW'" (M3-B). **D-Flow FM is the current M3 solver.** The `inflow.base_flow` schema bullet
> below still stands.

`CLAUDE.md` and `environment.yml` had assumed Delft3D FM (`hydrolib-core`, `meshkernel`,
`dfm_tools`). Checking the machine that will run it found only a GUI-only Delft3D 4.07.02
install (`kernels/` empty, no D-Flow FM kernel anywhere on disk). Asked the user; decided:

- **Solver: Delft3D 4 FLOW** (structured grid — `.grd`/`.enc`/`.dep`/`.rgh`/`.mdf`/`.bnd`/`.bct`/
  `.obs`), not D-Flow FM. `hydrolib-core`, `meshkernel` and `dfm_tools` are FM-only and don't write
  these formats; dropped from `environment.yml` (confirmed nothing in the repo imports them yet).
  `CLAUDE.md`'s M3 row, Stack line and "Simulation tools" line updated to match; the FLOW kernel
  path/version stay `NOT STATED` until the user installs `d_hydro`/`flow2d3d` (the C:\ install has
  the GUIs — RGFGRID, QUICKIN, the FLOW GUI, QUICKPLOT — but not the compiled kernel).
- **Kernel source:** the user installs the Windows FLOW kernels; the case is run from WSL through
  Windows interop. Not built from Deltares' open-source Fortran source in WSL (would need
  gfortran/MPI/netCDF-Fortran toolchain not currently set up, and a slower path with no clear
  benefit here).
- **Pilot build:** built by hand in the Deltares GUIs (RGFGRID/QUICKIN/FLOW GUI), from a written
  recipe, not by a throwaway script and not by `backend/m3_delft3d/generator.py` itself — so the
  reproduction test (`m3_spec.md`, Phase 3) compares against a reference that's independent of the
  generator, in Deltares' own file formatting.
- **Pilot extent:** a pilot-only site config (`m3_pilot/inputs/teesta_pilot.yaml`), not
  `sites/teesta.yaml`. The real Teesta far-field bbox at 30 m resolution is 2534×5016 (12.7M)
  cells — impractical for a hand-built pilot on 16 GB RAM (CLAUDE.md rule 13). The pilot uses a
  reduced South-Lhonak-to-Chungthang reach at a coarser resolution (~90 m, to be confirmed against
  cell count once M1 runs on it). `sites/teesta.yaml` itself is unchanged.
- **Base flow schema:** `domains.<domain>.inflow.base_flow`, an optional `DischargeValue`
  (`backend/shared/site_config.py:186`) alongside the existing `inflow.location`. Reuses the
  existing SourcedValue pattern rather than adding a new top-level block; `additionalProperties:
  false` preserved, existing site configs stay valid without it.

## 2026-09-25 — M4 pilot case: `backend/m4_pilot/` is calibration logs only, not a template case

Starting `backend/m4_sph/generator.py`, found `backend/m4_pilot/` holds only
`vram_estimator.py` and three logs from a run (`gencase_output.log`, `dualsphysics_output.log`,
`nvidia_smi.log`) — no GenCase `_Def.xml`, no STL, no launch script. The logs are from
DualSPHysics 5.4.3's own stock example `examples/main/01_DamBreak/CaseDambreakVal2D` (found on
the machine at `/mnt/d/APPS/DualSPHysics_v5.4/`, not checked into this repo). Decided (with user,
same session):

- **Inlet flow:** `backend/m4_sph/generator.py`'s `build_nearfield_case` accepts any
  `(t_s, q_m3s)` hydrograph. When `near_field.inflow.from` is a dam id, it uses M2's
  `hydrograph()`. When it's `far_field` (Teesta today), it raises `InflowUnavailable` — the SPH
  inlet needs a routed far-field discharge series that only M3 can produce; Teesta's near-field
  case can't be built until M3 exists.
- **SPH settings location:** `config/m4_sph.yaml` (dp, time window, inlet size, VRAM budget/
  margin), loaded into a validated `SphSettings` dataclass, project-maintained like
  `config/manning_n.csv` — not a site fact, no contract change. The contract itself notes (§4.4)
  that M3/M4 simulation settings aren't in `SiteConfig` yet.
- **Inlet geometry:** a fixed vertical rectangle (`inlet_width_m` x `inlet_height_m`), centred on
  `domains.near_field.inflow.location`, bottom at bed elevation from `dem_nearfield.tif`, rotated
  to the local channel tangent (`centreline.gpkg`) via DualSPHysics's inout-zone `rotateaxis`
  (as used in `examples/inletoutlet/05_ShapesInlet3D`). `v(t) = Q(t) / (W*H)`, free surface held
  fixed at bed + H (caveat `fixed_area_inlet` — flow depth doesn't vary at the inlet).
- **Regenerating the pilot case exactly** (`tests/m4_sph/test_pilot_regen.py`, diffing against a
  copy of the real `CaseDambreakVal2D_Def.xml`) validated the XML writer (`case_xml.py`) before
  it was trusted for real near-field cases. It's 2D with no STL/inlet, so it only checks the
  writer, not the terrain/inlet/probe logic — that's covered separately by a synthetic 3D
  near-field case (`tests/m4_sph/test_generator.py`) and, when `DSPH_BIN_DIR` is set, by actually
  running GenCase on both cases (`tests/m4_sph/test_gencase_smoke.py`).
- **GenCase caught two real bugs** the schema alone wouldn't have: a near-field case needs
  `hswl` set explicitly (not `auto`) because it starts with zero fluid particles (all inflow
  comes from the inlet zone at runtime) — `auto` would compute a still water level of 0 and zero
  out the speed of sound; and boundary particles' `mk` must stay inside `mkconfig`'s declared
  `boundcount`. Both fixed in `generator.py`.
- **Not yet verified:** the `rotateaxis` angle's sign convention (whether the inlet's imposed
  flow direction ends up pointing downstream or upstream) — GenCase accepts the XML, but
  confirming the actual flow direction needs visual inspection of a generated case (VTK), out of
  scope for this session. `InletGeometry`'s docstring flags this.

## 2026-09-25 — M4 pilot files moved into `backend/m4_sph/`; M4 post-processing added

Before this session, the user moved `vram_estimator.py` and its three calibration logs from
`backend/m4_pilot/` straight into `backend/m4_sph/` (the "M4 pilot case" decision above described
`backend/m4_pilot/` as calibration-only, not a template case — this consolidates it into the real
module instead of keeping a near-empty sibling folder). Fixed up the fallout: `generator.py`'s
`from backend.m4_pilot import vram_estimator` → `from backend.m4_sph import vram_estimator`;
`tests/m4_pilot/test_vram_estimator.py` moved to `tests/m4_sph/` with its import updated;
`CLAUDE.md`'s "Working pilot cases" line updated. `backend/m4_pilot/`/`tests/m4_pilot/` no longer
exist. `PILOT_DIR = Path(__file__).parent` in `vram_estimator.py` already resolved correctly
without changes.

**Update, same session:** on review, moved the three raw logs (not `vram_estimator.py`, which the
real generator imports and which stays in `backend/m4_sph/`) back out to a recreated
`backend/m4_pilot/`, kept as calibration provenance separate from the module's own code —
`PILOT_DIR` in `vram_estimator.py` now points there (`Path(__file__).parent.parent / "m4_pilot"`).
A general `*.log` `.gitignore` rule was added for everything else, with these three as an explicit
exception. **2D-vs-3D calibration caveat:** the pilot run is `CaseDambreakVal2D` (`Data2D=[1]` in
its own log) — a 2D case. `bytes_per_particle`/`bytes_per_cell` come straight from DualSPHysics's
own per-run "GPU Memory" report, so those should hold regardless of dimensionality, but
`cells_per_particle` (a 2D cell-linked-list needs far fewer cells per particle than a 3D one at
the same spacing) is 2D-specific and likely **undercounts** a real 3D near-field domain, compounding
the already-known boundary:fluid ratio gap. `vram_estimator.py`'s own docstring is updated with
this. This was the pre-M4-1 caveat; the stock 3D benchmark calibration and its remaining
terrain-specific limitation are recorded below.

**M4-1 update, 2026-09-27:** Teesta's M1 near-field terrain exists, but its configured inlet is
`far_field` and the available M3 pilot has no routed discharge series at that inlet. The M4
generator correctly raises `InflowUnavailable`; no substitute Teesta hydrograph was invented.
The fallback benchmark is the stock 3D `examples/main/01_DamBreak/CaseDambreak_Def.xml` from
DualSPHysics 5.4.3, generated with GenCase and run on the RTX 4060 Laptop GPU. The original
geometry and settings were retained; only `dp` changed. This calibrates 3D solver memory counts,
but does not validate the generator's terrain STL, boundary:fluid ratio, inlet, or a full Teesta
near-field case. The stock example was run directly through GenCase because the M4 Python
generator currently has no stock 3D benchmark builder (its writer pilot is 2D).

| `dp` (m) | particles (total) | peak VRAM (MiB; baseline → peak) | solver runtime (s) | wall time (s) | disk (bytes) |
|---:|---:|---:|---:|---:|---:|
| 0.0200 | 17,446 | 581 → 677 | 9.37 | 9.77 | 92,554,114 |
| 0.0150 | 37,896 | 572 → 695 | 17.31 | 18.10 | 200,431,814 |
| 0.0125 | 60,887 | 490 → 649 | 20.05 | 20.67 | 321,739,608 |

Each run directory under `backend/m4_pilot/dambreak3d_dp*/` keeps `gencase_output.log`,
`dualsphysics_output.log`, `nvidia_smi.log` (1-second samples), the edited source definition,
and `run_summary.json`. Large solver outputs remain local and are Git-ignored. The estimator's
default calibration now uses these three 3D runs, count-weighting particle and cell memory and
using the median measured CUDA/context overhead; the original 2D root logs remain available for
comparison.

Across the 3D runs, `bytes_per_particle` is 163.96 B, down 8.52% from the 2D calibration's
179.24 B. `bytes_per_cell` is 35.11 B (up 119.4%), while measured `cells_per_particle` is 0.07141
(down 93.4% from 1.08185). Thus the particle-plus-cell term falls from 196.55 to 166.47 B per
particle (−15.3%); median context overhead is 116.98 MiB versus 155.06 MiB. The stock 3D case's
cell ratio is lower than the 2D case, contrary to the earlier expectation. Its compact rectangular
geometry is not representative of a terrain-cut near-field domain, so retain the terrain
calibration caveat and revisit it when the routed inlet series is available.

Then added M4 post-processing (contract §4.4): `summary_nearfield/*.tif`, `surfaces/*.glb`,
`timeseries.csv`, `run_meta.json` — turning a completed near-field run's raw solver output into
the same schema a Delft3D run would produce. New files: `backend/m4_sph/{measuretool,gauges,
surfaces,vtk_polydata,gltf_writer,postprocess}.py`. Everything below was checked against the real
`MeasureTool_linux64`/`IsoSurface_linux64` binaries (`/mnt/d/APPS/DualSPHysics_v5.4/bin/linux/`,
which run fine under WSL) on the real pilot dam-break particle data before being written — none of
it is guessed from `-h` text or the PDF guides alone.

- **`VelocityGauge` tag bug fixed**: `case_xml.py` wrote `<vel name="...">`; the real tag,
  confirmed against DualSPHysics's own `examples/others/GaugeSystem/GVel_Dam2d.xml`, is
  `<velocity name="...">`. The wrong tag would have silently produced no `GaugesVel_*.csv` output
  from any real solver run — worth catching now since post-processing depends on that file.
- **Near-field domain = wherever `dem_nearfield.tif` is valid.** There's no
  `domain_mask_nearfield.tif` in the contract; this matches the test `generator.py` already uses
  to place probes and the inlet, so no new raster was needed.
- **`MeasureTool -elevation` column semantics, verified against real output, not assumed from
  `-h`:** a `POINTSENDLIST` block's z-candidates collapse into ONE elevation reading per block
  (not one per candidate) only if consecutive candidates are within the run's particle smoothing
  length of each other. A dry column (no fluid anywhere in its range) reports its elevation as
  exactly `z0`, the bottom of the range — not the `-kcdummy` fallback (`-kcdummy`/`-kcusedummy`
  turned out to have no effect in `-elevation` mode at all, only in `-vars` interpolation).
  Anchoring `z0` at the cell's own bed elevation, and sizing the candidate step as
  `elevation_dz_dp_fraction * dp_m` (`config/m4_sph.yaml`, default 0.5) rather than a fixed metre
  value, gets both properties for free: a dry column's depth is exactly `0.0` with no dummy
  handling, and the step scales with whatever the case's own smoothing length turns out to be
  (found the hard way — a fixed `elevation_dz_m: 0.5` silently produced one output column per
  candidate instead of one per cell, on the pilot data's real `dp=0.01 m`, `h≈0.028 m`; `dz=0.02`
  collapsed correctly, `dz=0.05` didn't).
- **SPH velocity: depth-average, not a fixed-height point** (approved by the user this session):
  `summary_nearfield/max_velocity.tif` samples `velocity_levels` explicit points per wet cell,
  evenly spaced from bed to that cell's own max depth (from the elevation pass), and at each
  timestep averages the horizontal speed over the levels at or below that timestep's elevation —
  so it's the same physical quantity as Delft3D's depth-averaged velocity, not a single point
  sample at a fixed height. Comparing a depth-averaged value against a fixed-height point would
  read as model disagreement when it's really a definition mismatch.
- **`summary_nearfield` time window = the SPH run's own `sim_duration_s`, not the whole record**
  (approved by the user this session): a Delft3D run's own near-field maxima should be computed
  over the same window as the matching SPH run's `sim_duration_s` (a longer Delft3D window would
  just have more chances to hit an extreme, biasing `sph_vs_delft3d` for no physical reason) — an
  M3-side change for a future session; today's work makes `run_meta.json`'s existing
  `sim_duration_s` field the thing to align on, rather than adding a new field for it.
- **Arrival threshold stays 0.1 m** (approved by the user this session), unchanged from the
  project-wide default — coarsening it just for SPH would desync its arrival numbers from every
  other output keyed on the same threshold. Every near-field SPH run instead gets a new caveat,
  `sph_arrival_below_resolution`, since particle spacing is always far coarser than 0.1 m; a
  second new caveat, `sph_depth_search_capped`, fires only if a cell's depth comes within 1.5
  candidate steps of the search ceiling (`bed + inlet_height_m`), flagging a likely underestimate.
  Both added to the contract's standard caveat ID list (§2.4).
- **Probe `timeseries.csv` comes from the solver's own real-time gauges, not MeasureTool** — the
  `swl`/`velocity` gauges `generator.py` already places per probe write `GaugesSWL_<name>.csv`/
  `GaugesVel_<name>.csv` themselves during the run; `gauges.py` just reads them. `GaugesSWL_*.csv`'s
  format was checked against a real pilot run's output; no real `GaugesVel_*.csv` example existed
  to check against (the pilot case has no velocity gauges — only fixed after this session's tag
  fix would a real run even produce one), so its parser reads columns positionally rather than by
  an assumed exact label.
- **glTF written by hand, no new dependency**: `vtk_polydata.py` reads `IsoSurface -saveiso`'s
  legacy VTK BINARY POLYDATA output (big-endian, per the legacy VTK spec — verified against a
  real file, not assumed); `gltf_writer.py` writes a minimal one-mesh `.glb` (glTF 2.0's binary
  container is simple enough to hand-write for a geometry-only mesh). `settings.surface_interval_s`
  (default 300 s, approved by the user) controls how many of the run's PART-cadence surfaces get
  kept, since keeping one glTF per PART would be far more than the 3D view's `max_payload_mb` needs.
- **`contracts/schemas/run_meta.schema.json` added** — `run_meta.json` existed in the contract's
  prose (§4.4) but had no schema file; formalized it now since this session is the first to write
  real ones. Added `caveats`/`has_placeholders`/`placeholder_fields` to the contract's own
  illustrative example alongside it, matching what `build_run_meta` actually produces (every
  result carries caveats per CLAUDE.md rule 10; `run_meta.json`'s existing snippet just hadn't
  shown any yet).
- **Real-binary tests**: every new module has both offline unit tests (literal CSV/VTK fixtures,
  no binary needed) and a real-binary test gated on `DSPH_BIN_DIR`, mirroring
  `test_gencase_smoke.py`'s pattern — run against the pilot dam-break's own particle data (already
  on disk, no solver run needed) rather than a hand-run near-field case, since running the actual
  GPU solver is out of scope for a Claude session (CLAUDE.md rule 14). One test's hand-built
  7-cell near-field grid deliberately places a cell at `x=3.8` near the dam-break box's far wall;
  it happens to catch a real run-up wave that comes within the `sph_depth_search_capped` margin —
  left the assertion matching that real behaviour rather than picking a ceiling that avoids it.
- **Not done this session (next session, per the user):** `/compare`'s real `sph_vs_delft3d`
  section is still the `compare.example.json` mock (`backend/m0_api/compare.py` only fills
  `emulator_vs_physics`/`gp_vs_linear` today); `styles.json` doesn't have `velocity_diff`/
  `arrival_diff` yet (only `depth_diff`); and the mock's `available: true` with all-zero metrics
  should become `available: false` when no run exists, so a demo can't misread "no run yet" as
  "no flooding predicted".

## 2026-09-26 — M3: ANUGA replaces Delft3D 4 FLOW (DECIDED with user this session)

> **SUPERSEDED the same day** by "M3: back to Delft3D FM, superseding 'ANUGA replaces Delft3D 4
> FLOW'" (M3-B, immediately below). **D-Flow FM is the current M3 solver; ANUGA
> (`backend/m3_pilot/`) is kept as a fallback for the record, not the M3 reference.**

**Decision:** the M3 far-field flood solver is ANUGA (`anuga` 4.0.1 from PyPI, unstructured
triangular finite-volume shallow-water solver), not Delft3D 4 FLOW.

**Why**
- The free Delft3D 4.07.02 package is GUI-only: `kernels\x64\bin\` is empty, and the README says the
  FLOW kernel must be compiled from Deltares' source. The classic Delft3D 4 kernel source sits on
  Deltares' registration-only SVN. No build recipe exists that targets the 4.07.02 GUIs, and the only
  gfortran recipe found dates from 2013 (Ubuntu 12.04). Compiling it was judged out of budget.
- ANUGA installs with `pip install anuga` in the project venv: prebuilt wheels, no compiler, about
  12 s. It runs in WSL and is scriptable end to end from Python, with no GUI step.
- The Phase A spike (coarse Teesta pilot, 9.7 k triangles, 30 000 s simulated) ran in 33 s wall time
  with a 180 MB peak, far inside the hardware budget (CLAUDE.md rule 13).
- The spike did not route the flood. It pooled below the breach, and the cause was mesh coarseness in
  a narrow gorge, not the solver. The frozen pilot fixes this with corridor refinement
  (`backend/m3_pilot/teesta_pilot_s001__anuga.py`, `docs/m3_spec.md`).

**Consequences**
- **Pilot:** `backend/m3_pilot/teesta_pilot_s001__anuga.py` is a hand-written, self-contained ANUGA
  script and becomes the independent reference for the generator reproduction test. It replaces the
  hand-built Delft3D GUI recipe. The Delft3D pilot artefacts (`.grd`, `.enc`, `.d3d`, `RECIPE.md`,
  grid NetCDF) were deleted. The input exporter was kept as provenance at
  `backend/m3_pilot/inputs/make_export.py`.
- **Module:** `backend/m3_anuga/` replaces the never-written `backend/m3_delft3d/`.
- **Contract (PENDING sign-off, not yet changed):** the run_id `model` enum (`delft3d | sph`,
  handoff contract §1.7, `run_meta.schema.json`) needs `anuga`. The change will be shown for
  approval before Phase 2 uses it.
- **Arrival time in wet-channel runs (PROPOSED clarification, UNCLEAR until the contract is updated):**
  with `inflow.base_flow` set, channel cells are already deeper than `arrival_m` = 0.1 m at t0. M3
  therefore records arrival as the first t ≥ 0 at which depth exceeds the t0 depth by more than
  0.1 m. For cells dry at t0 this is identical to the contract rule.
- **Deferred:** the wider clean-up of Delft3D references (CLAUDE.md M3 row / Stack / Simulation
  tools, handoff contract, M4 `sph_vs_delft3d` comparison names, job-state wording, the
  `teesta_pilot.yaml` header) is a separate pass.
- **Known limitation:** ANUGA, like Delft3D, is clear-water. Debris/sediment-laden flow is not
  represented (CLAUDE.md "Known limitations").

## 2026-09-26 — M3: back to Delft3D FM, superseding "ANUGA replaces Delft3D 4 FLOW" (M3-B, DECIDED with user this session)

**Decision:** the M3 far-field solver is **D-Flow FM** (Delft3D DIMRset 2026.01), not ANUGA. ANUGA
is kept as a fallback for the record (`backend/m3_pilot/`), not the M3 reference.

**Why:** the SIH problem statement requires a Delft3D-vs-SPH comparison, which ANUGA can't
provide. The reason ANUGA was chosen over Delft3D — no runnable Delft3D kernel — no longer holds:
a D-Flow FM kernel now builds and runs from Deltares' own unmodified source
(`docs/dflowfm_kernel_build.md`; D-Flow FM 1.2.184 + DIMR 2.00, Intel oneAPI 2024.2, built in WSL),
verified against Deltares' own example and D-Flow FM tutorial06 (0 errors) and against a case
written by hydrolib-core (0 errors).

**`model: "delft3d"` now means D-Flow FM.** No contract/schema change: `run_meta.schema.json` and
`scenario_design.schema.json` already have `"delft3d"` in their `model` enum (added when the plan
was the classic FLOW kernel); it is repurposed rather than renamed, since `docs/handoff_contract.md`
§1.7's `run_id`/`model` patterns, `backend/m0_api`'s `runs` table, and `backend/campaign.py`
(`model: "sph"` sibling) all already use the string `"delft3d"` and none of that needs to change.
`docs/handoff_contract.md` §1.7 now has a one-line note next to the `model` row saying so.

**Consequences**
- **Module:** `backend/m3_delft3d/` (never written) is M3's home again, building D-Flow FM cases
  with hydrolib-core 1.4.0 + meshkernel 8.3.0 and reading output with dfm_tools 0.47.0/xugrid
  (pinned in `environment.yml`/`requirements.txt`, verified against the built kernel with no
  version drift in anything already installed — `docs/dflowfm_kernel_build.md`).
- **Pilot:** `m3_pilot/`/`m3_cascade_pilot/` (D-Flow FM) are created in M3-1/M3-4. The ANUGA pilot
  at `backend/m3_pilot/` (`teesta_pilot_s001__anuga.py` + its exporter) stays as a kept-for-record
  fallback, not the M3 reference — `CLAUDE.md`'s "Working pilot cases" bullet updated to say so.
  `backend/m3_pilot/inputs/teesta_pilot.yaml`'s header rewritten to record both solver pivots
  honestly (Delft3D 4 FLOW GUI plan → ANUGA → D-Flow FM) rather than only the most recent one.
- **Rules for M3 (M3-1 case generation, M3-3 launch/post-processing, M3-5 run budget) —** learned
  building and running the kernel this session, to be copied into `docs/m3_spec.md` /
  `docs/run_budget.md` once those files exist:
  1. **Run success** = the `.dia` has no line starting `** ERROR` AND the expected `*_map.nc` and
     `*_his.nc` exist in the output dir. Never trust the exit code alone: `run_dflowfm.sh` exited
     0 in three separate runs where the kernel actually rejected its input.
  2. **`.ext` version:** any `.ext` file written by hydrolib-core must have `fileVersion = 2.01`
     (`ExtModel.general.fileversion = "2.01"`). hydrolib-core 1.4.0 writes 3.00, and this kernel
     then logs `Unsupported format … Ignoring this file` and silently runs with no boundaries.
  3. **Net-file writer:** hydrolib-core's net writer failed on an old (2015) real-world net
     (meshkernel dropped 1 of 8916 nodes on read; the writer then tried to write `node_z`'s
     original 8916 values against the reduced node count and raised a shape mismatch). Before
     trusting the writer for real cases, M3-1 must show on our own meshkernel-built meshes that a
     written net re-reads with identical node/edge/face counts and that the kernel runs it.
  4. **Paths:** case files reference each other with relative paths so cases are relocatable.
  5. **Output size:** D-Flow FM tutorial06 wrote an 805 MB `_map.nc` for a 10-day run at default
     map settings (8355 cells, 1200 s map interval, default `Wrimap_*` variables). M3-1 sets
     `MapInterval` and the `Wrimap_*` switches to only what post-processing needs (summary maps:
     max depth, max velocity, arrival time, …), and `docs/run_budget.md` must list **disk per
     run**, not just wall time and RAM.
  6. **Running the kernel:** strip `/mnt/*` from `PATH` before running it (the Windows dirs WSL
     adds otherwise make cmake — and presumably other tools — pick up Windows-side packages
     instead of the Linux ones; use `run_dflowfm.sh` for plain MDUs and `run_dimr.sh -m
     dimr_config.xml` for DIMR configs). Runs launch as detached jobs (CLAUDE.md rule 14).
- **P5 (proposed, DEFERRED to M3-1):** moving solver-independent code from the ANUGA pilot into
  `backend/m3_common/` was proposed and not applied. On inspection, three of the four proposed
  pieces don't actually exist as reusable code yet: the pilot reads its own flat-file export
  (`.pol`/`.xyz`/`.tim`, hand-portable-format-specific), not M1's real Python output, so the real
  generator should read M1 directly instead (as `backend/m4_sph/generator.py` already does); its
  POI/gauge sampling is ANUGA-mesh-specific ("triangle containing point"; D-Flow FM's meshkernel
  mesh needs its own version of the same pattern); and it has no canonical-grid/`run_meta.schema.
  json` post-processing at all (it writes triangle-centroid CSVs and its own ad-hoc
  `run_meta_pilot.json`) for M3-1 to build on `backend/m4_sph/postprocess.py`'s patterns instead.
  Only `inflow_point()` (DEM priority-flood + closed-basin spill-point relocation) and
  `discharge(t)` (edge-clamped hydrograph sampling) are genuinely solver-independent as-is. Rather
  than move code that would save M3-1 little or nothing, `backend/m3_common/` (spill-point
  relocation + hydrograph sampler) will be **new-written from the pilot's logic, with tests, in
  M3-1 Phase 2** when the real generator needs it — not moved ahead of that need.
- **Known limitation, unchanged:** clear-water, not debris/sediment-laden (CLAUDE.md "Known
  limitations"); the moraine/embankment breach-equation caveats already in the contract still
  apply regardless of far-field solver.

## 2026-09-27 — Keep M3 pilot artifacts under `backend/`

The D-Flow FM pilot implementation, case, and diagnostic attempts live in
`backend/m3_pilot/dflowfm/`. The ANUGA record and shared pilot exports remain in
`backend/m3_pilot/`. The builder derives the repository root from its own path and writes its
case under `dflowfm/`; relative solver paths keep the case relocatable. A copy of the moved case
ran from a temporary location with no `.dia` errors and produced map and history outputs, without
changing the kept pilot outputs.

## 2026-09-27 — Scene3D response format

M0's Scene3D response uses raw float32 little-endian row-major height grids for terrain and the
emulated median water-surface elevation. It shares M1's near-field local metric origin with SPH
meshes; vertical exaggeration is renderer metadata applied on Z, preserving physical metre values.
The aggregate referenced binary asset budget is below 20 MB decimal, leaving 50 kB for JSON metadata. Terrain is downsampled, while up to 128 SPH surface
snapshots are included in time order only while the combined scene assets fit the budget. M0 can
build a Delft3D near-field water surface from the paired near-field DEM and maximum-depth raster.

## 2026-09-30 — Feature 1: site registry and onboarding (DECIDED with user)

1. **Bundled sites** are listed in `config/sites.yaml` (`bundled: [teesta]`). Every other site is
   registered at runtime by `POST /sites` and persisted at `data/<site_id>/config/<site_id>.yaml`
   *before* its onboarding job is queued (`backend/m0_api/site_registry.py`). `KNOWN_SITE_IDS` is
   gone. No new SQLite tables (contract §4.5 unchanged).
2. **Fresh-clone Teesta status** is `onboarding` / `configured_not_onboarded` until a real
   onboarding job completes; status is derived from the site's latest onboarding job
   (`job_stage_<stage>`, `failed` + the job's error code, `ready`). Site list/detail are built
   from the saved config; the contract examples are no longer used for sites.
3. **Rishi Ganga** gets no special case: generic `site_not_found` until someone onboards it.
4. **Duplicates:** 409 `site_already_exists` for any registered id or any repo `sites/*.yaml` id
   (except the test fixtures below); 409 `site_name_taken` for an exact duplicate name, ignoring
   case and extra whitespace.
5. **Test fixtures:** `TEST_FIXTURE_SITE_IDS = (demo_valley, synth_engdam)` may be onboarded
   through the API (the test suite does so); only `demo_valley` is routed to the I-1 synthetic
   path (`SYNTHETIC_FIXTURE_SITE_IDS`).
6. **Registration rule** beyond the schema: a non-empty `site.name` and a non-null far-field bbox
   (a placeholder value is fine) — `SiteSummary.bbox_lonlat` requires four numbers.
7. `load_site_config(site_id)` without `sites_dir` now looks in the repo `sites/` first, then the
   onboarded config under the data dir, so M1/M6/M7 CLIs and API helpers find onboarded sites
   without changes. A bundled site can never be shadowed by a data copy.

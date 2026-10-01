# Progress log

## 2026-09-27 — Frontend rebrand: Sentriq → TerraFlow
- Branding-only rename of the user-facing product name from "Sentriq" to "TerraFlow": page
  title (`frontend/index.html`), sidebar/landing wordmark and avatar initials, eyebrow/footer/
  dialog brand text (`frontend/app/sentriq/app.tsx`, `frontend/app/sentriq/landing.tsx`),
  `frontend/README.md`, `frontend/STYLE_GUIDE.md` prose, and `frontend/package.json` `"name"`
  (`sentriq-frontend` → `terraflow`, nothing else referenced it).
- Left all code/module identifiers untouched per scope: the `frontend/app/sentriq/` folder,
  `SentriqApp` component name, `.sentriq-sidebar`/`.sentriq-logo` CSS classes, and the
  `frontend/lib/sentriq.ts` file path — these are file paths and code identifiers, not
  user-facing branding. The dated 2026-09-24 log entry below describing the retired "Sentriq"
  prototype is left as a historical record.
- No product-name text existed in `backend/m0_api`'s FastAPI app title or in the PDF/KML/shp
  export code (`backend/m0_api/main.py`, `mock_files.py`) — nothing to rename there.
- Added a `SENTRIQ|Sentriq(?!App)` forbidden-string check to
  `frontend/scripts/check-shell.mjs` (scoped to exclude the surviving lowercase folder/CSS/
  component identifiers) so the retired brand text can't silently reappear.
- Verified: `tsc --noEmit`, `vite build`, `check:shell`, the full Playwright visual suite (all
  13 baselines re-recorded — the only diffs were the brand text itself, no layout/overflow
  regressions), and `pytest tests/m0_api` (169 passed, 1 pre-existing hardware-gated skip).

## 2026-09-27 — M3 detached D-Flow FM launcher
- Added `backend/m3_dflowfm/launcher.py`: starts the kernel detached, strips `/mnt/*` from `PATH`,
  writes combined process output to the M0 run log, parses simulation-time markers from `.dia`, and
  evaluates success only by M3 rule 1 (`.dia` has no `** ERROR` line and map/history outputs exist).
- Connected M0 campaign jobs carrying a `case_dir` payload to the D-Flow FM launch/poll/recovery
  path; other jobs retain the existing fake solver path. The kernel return code is not consulted.
- MPI check: the reference `.dia` says `MPI : no` and `OpenMP : unavailable`; this kernel bundle
  has no MPI launcher or partitioner, and its parallel-MDU helper requires partitioned network
  files. Partitioned MPI cannot be run here, so serial remains the supported mode (details in
  `docs/m3_spec.md`).
- Added focused launcher contract tests. `py_compile` and direct success/progress assertions pass;
  pytest could not initialize because this machine's active Python lacks `geopandas`, imported by
  the M3 test conftest before test selection.

## 2026-09-27 — M3 mesh-spacing correction and Teesta generated-run check

- Reconciled the earlier blocker report against the current tree: D-Flow generation is in
  `backend/m3_dflowfm/` (not `backend/m3_delft3d/`), shared pilot post-processing is in
  `backend/m3_common/`, and M0 detached launch/retry plus M5 scenario-design/LOOCV code exist.
  `docs/m5_specs.md` is present; the README's singular `docs/m5_spec.md` reference is stale.
- Fixed a concrete generator defect: the nominal mesh spacing was only used for simplification
  and corridor widths, while long polygon edges were passed directly to MeshKernel. Added
  boundary segmentization at the requested spacing. Before the fix, the generated 90 m mesh had
  3,234 faces, median edge 185 m, p90 504 m, max 8.30 km. A 30 m trial failed the existing
  orthogonality gate (`max cos(phi)=0.996558`), so it was not launched.
- Generated and launched one corrected 90 m Teesta-pilot case as a detached real D-Flow FM run.
  It ran 30 simulated hours, with no `** ERROR`, and wrote map/history outputs. Net: 77,415 faces,
  round-trip exact. Runtime 994.7 s, peak RSS not captured, raw map ~1.13 GB. Post-processing
  wrote 623 x 611 far-field rasters; `run_meta.json` validated against `run_meta.schema.json`.
  Max depth 67.38 m, max velocity 33.14 m/s, 1,878 wet in-domain cells. Inputs remain pilot
  placeholders (`has_placeholders=true`).
- The generated run does not pass the smoke acceptance gate: all four POIs are dry, including
  Chungthang, while the frozen pilot reaches it (11.13 m depth, 5.16 m/s, arrival 55,860 s). The
  nearest generated wet face is 3.70 km from Chungthang. The generated run includes only South
  Lhonak; the configured Teesta III cascade needs a routed discharge series and a sourced trigger
  threshold, but `sites/teesta.yaml` leaves `teesta_iii.trigger.value` null/placeholder. Pilot and
  M1 terrain/domain inputs also differ, so this is the known mismatch, not yet an isolated single
  cause. `docs/m3_reproduction.md` retains the failed status and records the run.
- Added a focused boundary-spacing unit test; `tests/m3_dflowfm/test_generator.py`: 4 passed.
- No scenario campaign was started: the required pilot/smoke gate failed. No new campaign-run
  failures or retries occurred. The old source-boundary `.dia` errors remain historical artifacts.
- Remaining decisions/data before proceeding: source/approve the Teesta III trigger input for
  cascade campaigns; agree a contract artifact for M3-routed discharge consumed by M4 (contract
  §4.4 `timeseries.csv` currently contains POI depth/velocity/WSE only); provide Chamoli/Rishi
  Ganga site/event inputs; and set a resource-valid production plan. No emulator scores can be
  claimed until accepted real training runs exist.

## 2026-09-27 — bounded-memory M3 post-processing

- Changed `backend/m3_common/postprocess.py` to reduce map depth and velocity in bounded time
  chunks. It keeps per-face maxima and first-arrival times without materializing the full
  timestep-by-face arrays. Chunk working arrays are capped near 128 MiB, independent of the
  number of stored map timesteps.
- Added a reduction-equivalence unit test. `tests/m3_common/test_postprocess.py` and
  `tests/m3_dflowfm/test_generator.py`: 9 passed.
- Reprocessed the completed 77,415-face Teesta 90 m pilot with the chunked path. The resulting
  `run_meta.json` validates against the contract schema; run metrics remain 994.7 s solver
  runtime, 1.13 GB raw map, 67.38 m maximum depth, 33.14 m/s maximum velocity, and 1,878 wet
  in-domain cells. POI acceptance remains failed (all four POIs dry), so this is not a production
  smoke pass and no campaign jobs have been started. No campaign status registry/file is present.
- Production peak memory/disk remain unmeasured and require an output-size preflight. This
  removes the previously identified unbounded timestep-array allocation, but does not change the
  failed physics acceptance or unlock site campaigns.

## 2026-09-27 — M3 pilot status and next step

- **Built and reusable:** `backend/m3_pilot/` holds the frozen Teesta D-Flow FM case, inputs,
  builder, pilot metrics and historical attempts (plus the retained ANUGA record). Production
  execution lives in `backend/m3_dflowfm/`; M0 detached launch/recovery and one retry, shared
  contract post-processing, scenario/campaign infrastructure, and M5 design/LOOCV code already
  exist elsewhere in `backend/`. Fixed the generated mesh boundary spacing and bounded the M3
  postprocessor's map reductions; focused tests pass (9/9).
- **Still placeholder or incomplete:** Teesta site/breach inputs and Teesta III trigger remain
  placeholders; the generated real 90 m pilot has all four POIs dry, so it does not pass smoke
  acceptance. M3's contract timeseries is POI depth/velocity/WSE, with no agreed routed-discharge
  artifact for M4. `sites/rishiganga.yaml`, Chamoli terrain, and a sourced M3-compatible event
  input are absent; the 2021 rock/ice avalanche cannot honestly be encoded as a dam breach.
  Production resource/storage estimates remain unmeasured. No site campaigns, real-data M5
  training/LOOCV scores, or frontend emulator handoff are available.
- **Next:** source/approve the Teesta III trigger and decide the M3→M4 discharge artifact/location;
  resolve Teesta input/domain differences until one generated run wets the expected POIs, then
  run a small contract-valid smoke set. In parallel, provide Chamoli terrain and approve a
  source-backed event representation. Only after those gates should the recorded campaign design
  run serially, followed by per-site validation, M5 training/LOOCV, and frontend handoff.

## 2026-09-24 — backend/shared: site config loader + canonical grids
- `backend/shared/site_config.py`: Pydantic v2 model of the `sites/*.yaml` v1 format. Every value must carry
  `unit`/`source`/`status`; typed values check units, bbox/point ranges, UTM EPSG, enums, ISO dates; cross-checks ids,
  cascade `triggered_by`, inflow sources, near-inside-far and cell-size divisibility. `load_site_config()` emits one loud
  `PlaceholderWarning` (+ log) listing every placeholder path; `SiteConfig.placeholder_fields` / `has_placeholders`.
- `backend/shared/grid.py`: `CanonicalGrid` (= `grid.json`, contract §1.4), far/near grid builders (outward snapping,
  near nests on far-field corners), `lonlat_to_rowcol` / `rowcol_to_lonlat`, `resample_to_grid` (nearest for ints,
  bilinear for floats), `write_grid_raster` (tiled LZW GeoTIFF).
- Tests: `tests/shared/` on synthetic site `tests/fixtures/shared/synth.yaml` — 67 passing.
- Env: `environment.yml` (canonical), `requirements.txt` + local `.venv` (conda not installed on this machine).
- Teesta (all placeholders, 48 fields): far-field 2534 x 5016 @ 30 m (12.7 M cells, ~51 MB per float32 raster),
  near-field 906 x 1011 @ 10 m.
- Open: schema drift vs contract §3.1 logged in docs/decisions.md (pending team decision).

## 2026-09-24 — frontend: strip the Sentriq prototype to a UI shell (in progress)
Executing the approved plan from `frontend/STYLE_GUIDE.md` + `frontend/API_USAGE.md`'s review: strip the inherited
"Sentriq" prototype (Tehri-only, its own toy solver, Cloudflare D1/R2 + ChatGPT auth) down to a UI shell that keeps
every screen/component/style, with all data routed through one seam (`frontend/src/data/source.ts`) that returns
"awaiting" until the real M0 API exists. Branch `frontend-ui-shell`, commits reviewed by screenshot diff after each
group (`docs/strip_check/<group>/`, Playwright harness in `frontend/visual/`, kept out of the app's own deps).

- **Baseline** (`39fb6fa`): snapshot of the prototype as received. **Note:** a bare `data/` rule in the repo root's
  `.gitignore` (meant for the backend's gitignored `data/`) also matched `frontend/public/data/` and
  `frontend/src/data/` (no leading slash → matches any depth), so those never actually landed in `39fb6fa` — caught
  and fixed in `24f24b1` with `!/public/data/**` / `!/src/data/**` negations in `frontend/.gitignore`, before either
  path was deleted for real. Watch for this pattern if any other `frontend/**/data/` directory shows up later.
- **Group 1 — build swap** (`517b992`): replaced vinext/Next.js/Cloudflare Workers with a plain Vite + React SPA
  (`index.html`, `src/main.tsx`, minimal `vite.config.ts`). No data/content changes. Screens are pixel-identical to
  baseline except a 0.07% diff on the library screen's saved-runs badge (401 → unreachable fetch), expected and
  since resolved by Group 2 removing that endpoint entirely.
- **Group 2 — solver/data/cloud/offline removal** (`00011cc`): added `src/data/source.ts`; removed the Fast
  Screening solver, the Tehri scenario library and terrain, synthetic exposure, the GEE script generator,
  `/api/records` + D1/R2 + ChatGPT auth, IndexedDB/service-worker offline caching, and the Python toolkit. All 9
  upload buttons and the two offline buttons stay on screen, now `disabled`. `tsc`/`vite build` clean, 0 console
  errors on all 12 screens. Deferred to Group 3 (wording, not mechanical): the map's "Loading prepared terrain /
  The Tehri scenario library is loading." placeholder, sidebar's permanent "Loading…" label, the dataset registry's
  hardcoded "LOADED" badge and Tehri/THDC row text, and the methodology dialog's solver narrative — all visible
  in `docs/strip_check/02-data-solver-cloud-removed/`, not regressions.
- **Group 3 — remaining literals + stale awaiting-state text** (`0d3e9b9`): fixed the Group 2 deferred items above,
  plus every other Tehri/§10 literal found by a fresh repo-wide grep — `terrain-map.tsx`'s `grid.name.startsWith
  ('Tehri')` branches (inflow-pin label, 4 hardcoded map-place labels) and its Mapzen attribution, `landing.tsx`'s
  "4 cached runs" (now `nf(cache.length,0)` — `cache` was already an unused prop), "25–100% of 240 m", the
  hardcoded coordinate/grid-dimension captions, the "RUNS IN BROWSER · ready" badge on a solver that no longer
  runs anywhere, and the methodology dialog's description of "four real local-inertial screening runs over
  downloaded Tehri terrain" (rewritten to state what's connected and what each section will show once something
  is, rather than inventing new claims about the real M5 emulator's behaviour). Found and fixed a real bug while
  here: `landing.tsx`'s "Interpolating between X% and Y%" message compared `bracket?.lower?.severity ===
  bracket?.upper?.severity`, which is `undefined === undefined` → `true` whenever `bracket` is `null` — so it
  always read "Exact cached scenario" even with zero scenarios connected, in every build back to the original
  prototype. Layout unchanged (0% diff on 6 screens, ≤0.5% text-only on 5, +21px on `data` for two longer
  sentences).
- **Group 4 — map colours through `getStyles()`** (`54534a7`): `terrain-map.tsx`'s depth/velocity/arrival class
  breaks were hardcoded arrays; they now read an optional `styles` prop shaped like contract §6
  (`contracts/styles.json`), fetched once via `source.getStyles()` alongside terrain/scenarios. The CSS colour
  ramp stays; the numeric legend labels show "Awaiting style classes" until `styles` arrives. Verified by direct
  checks of the extracted band/label logic (fallback reproduces the original hardcoded values exactly; a
  contract-shaped example converts and dedupes correctly) rather than a screenshot, since the map component isn't
  reachable anywhere while `grid` stays `null` (confirmed: 0% diff on every screen).
- **Group 5 — SPH lab relabelled** (`069b52d`): kept per the approved decision (self-contained, already
  disclaims itself). Labelled "Educational Explainer" in the nav, page title, a new panel badge
  (`app/sph-lab.tsx`), and the two internal references to its old name, so the page doesn't call itself two
  different things.
- **Group 6 — guard script + docs**: added `npm run check:shell` (`scripts/check-shell.mjs`) —
  `tsc --noEmit` + `vite build` + a grep across `app/`, `lib/`, `src/`, `components/`, `hooks/` for six strings
  that name pieces Groups 1–5 removed (`tehri`, the hardcoded source-cell index `4510`, `model-worker`,
  `/api/records`, `oai-authenticated`, `240 m`) — verified it actually fails when one is reintroduced, then
  reverted the test. Removed the now-dead `db:generate` script (drizzle is gone) and the stale
  `"site-creator-vinext-starter"` package name (vinext is gone). Rewrote `README.md` for the shell's current
  state (dropped every demonstration step that named removed functionality). Refreshed `STYLE_GUIDE.md` (build/
  stack/routing sections only — the CSS/component documentation was untouched by the strip and stays accurate)
  and rewrote `API_USAGE.md` around the `source.ts` seam, what's disabled and why, and what still computes
  client-side and might belong in a backend `Estimate` instead.

## Where this leaves the frontend
A UI shell: every screen, layout, component and style unchanged, reading through one seam
(`frontend/src/data/source.ts`) that today always answers "awaiting" — honestly, not by hiding the fact. Two
`source.ts` functions (`listScenarios`, `listSavedRuns`/`saveRun`) have no matching endpoint in
`docs/handoff_contract.md` yet; four more (`getSite`, `getImpact`, `getCompare`, `getObserved`, `getJob`) are
defined but not called from anywhere — the impact and compare views still compute/import client-side and haven't
been rewired. Connecting the real backend should mean filling in `source.ts`'s function bodies with `fetch()`
calls and, for the disabled upload buttons (§3 of `frontend/API_USAGE.md`), deciding where each one's parsed
result should live now that there's no local solver or cache to hand it to.

## 2026-09-24 — contracts/ generated; M0 mock API serving every endpoint

Generated `contracts/` (JSON Schemas + one example per payload, plus `styles.json`)
from `docs/handoff_contract.md` §2, §4-§6 — this didn't exist before this session.
Built `backend/m0_api` (FastAPI) serving all 22 endpoints from contract §5 as mocks
backed by those examples; every response is validated against its schema before
being sent (and again independently in tests). Raster layers serve as PNG, vectors
as GeoJSON, exports as real zip/KML/PDF bytes with correct media types.

No job queue or `data/registry.sqlite` behind this yet — every well-formed ID
returns the same mock payload; only `site_id` is checked against a short known-sites
list (`teesta`, `rishiganga`) so 404 handling has a real path to test. Wiring real
M1-M7 outputs, the registry and job queue is future work.

`environment.yml` / `requirements.txt` gained `jsonschema`, `httpx`, `fastapi`,
`uvicorn`. `pytest -q`: 131 passed (64 new in `tests/m0_api/`).

Known gaps carried over from the earlier contract-conflicts review, still pending
the user's decisions: the site-config YAML-vs-§3.1 mismatch, and the frontend
`API_USAGE.md` conflicts list. Neither blocks this mock API, which only implements
what §5 already specifies.

## 2026-09-24 — M0 job system: registry, worker, detached runs, restart recovery

Replaced the mock `POST /sites` and `GET /jobs/{job_id}` with a real job system. All other
endpoints are still mocks.

- `backend/m0_api/registry.py`: `data/registry.sqlite` with the four §4.5 tables, using exactly the
  contract columns. WAL mode, so the API and the worker can share it. `SIH26_DATA_DIR` overrides
  `data/`.
- `backend/m0_api/jobs.py`: the state machine, using the **contract §5.3 stages as frozen**, not the
  pending decisions.md Part 2 proposal (user's choice this session). The worker bookkeeping §4.5 has
  no column for (`started_at`, `demo_mode`, `run_ids`, recent events) lives in `payload_json`, so
  the contract is unchanged. Writes check the expected current stage, so a stale writer is
  rejected.
- `backend/m0_api/worker.py` (`python -m backend.m0_api.worker`) is a separate process holding a
  single-worker lock.
  - In-process stages are FAKE (they sleep).
  - `simulating` launches runs one at a time as detached processes (`backend/m0_api/fake_solver.py`)
    and reads progress from each run's `log.txt`.
  - `recover()` at start-up re-attaches to live runs, records runs that finished while it was down,
    and fails the job with `worker_lost_run` if a run died unobserved.
- `eta_s` stays null until one run has finished; after that it is the mean wall time × runs
  remaining.

`pytest -q`: 174 passed (43 new). Also checked by hand with real uvicorn and worker processes:
killing and restarting the API mid-`simulating` loses nothing; killing the worker leaves the solver
running, and the restarted worker re-attaches and finishes the job.

Open gaps, all needing a team or contract decision:
- `POST /sites/{id}/rerun` is still a mock: contract §5.3 lists no stages for `rerun`.
- No retries and no minimum-run rule: `max_run_retries` / `min_runs_for_training` ⚙️ are unset.
  One failed run fails the job (`run_failed`).
- Contract §1.8 has no location for a job-level log; `log_tail` is built from `payload_json`
  events plus the tail of the active run's log.
- `POST /sites` now rejects a missing or invalid `site_config.site_id` (422) and a site that
  already has an active job (409 `site_onboarding_in_progress`); there is no uniqueness check
  against configured sites yet.
- `runner.is_alive` reads `/proc`, so it works on Linux/WSL only.

## 2026-09-24 — M0-5: render raster layers to PNG overlays (`rendering.py`)

`backend/m0_api/rendering.py`: colours a single-band raster (a canonical-grid array, e.g.
from a `queries/<query_id>/layers/*.tif`) into an RGBA PNG, driven entirely by
`contracts/styles.json` (§6) — no colour is hardcoded, so the frontend legend, this
renderer and M6's future KML export (M6-6) all read the same file. Handles every
`styles.json` shape: `continuous` (linear stops), `classes` (breaks + colour bands),
`diverging` (signed range about zero) and `extent_class` (HIGH/POSSIBLE fill + opacity).
Nodata and non-positive cells (dry ground / zero probability) are fully transparent.
PNG encoding uses GDAL's PNG driver via `rasterio.io.MemoryFile` (no Pillow — it isn't
in the project's stack). `render_and_cache` writes the PNG next to its source `.tif`
and reuses it on repeat requests, matching the §1.8 `queries/<query_id>/layers/` layout.

Bounds come from `CanonicalGrid.bounds_latlng` (`backend/shared/grid.py`, unchanged) —
the PNG's pixel grid stays in the site's UTM canonical grid and is stretched onto those
EPSG:4326 bounds by the Leaflet image overlay, rather than reprojected pixel-by-pixel;
noted as a simplifying assumption in the module docstring.

Wired into endpoint 11 (`GET /flood/{query_id}/layers/{layer_id}.png`, `main.py`): if a
real `.tif` exists on disk at the contract path it's rendered for real; otherwise the
endpoint keeps returning the 1x1 mock PNG, since M5 doesn't write real layer GeoTIFFs
yet (CLAUDE.md rule 2).

Contract check: §2.6 `LayerRef` has no per-layer `legend` field — §6 already states
`styles.json` is the one file driving overlays, KML styling *and* legends, i.e. the
frontend is meant to resolve `style_id` against `GET /styles` itself. Flagged this
against a literal reading of an initial task description ("include legend data in each
layer response") and the user confirmed: follow the contract as written, no `legend`
field added to any response.

Tests: `tests/m0_api/test_rendering.py` (9, synthetic grid + arrays — bounds, dry/nodata
transparency, classes/continuous colours checked pixel-for-pixel against
`styles.json`, extent_class fill+opacity, unknown layer_id raises, cache writes once
and is reused) + one new endpoint test wiring a real GeoTIFF through `GET /flood/.../
layers/*.png`. `pytest -q`: 184 passed (10 new).

Note: this machine's `.venv` is missing `jsonschema`/`fastapi`/`httpx`/`pip` itself;
`/usr/bin/python3` has the full stack instead and is what ran all tests this session.

## 2026-09-24 — M2 breach engine

Built `backend/m2_breach/` from scratch (it was an empty directory): one module per base
equation (`f16.py`, `xz9.py`, `z20.py`, `f95.py`, `f8.py`, `mclm.py`, `h14.py`), the Table 5/DFM
2024 fusion (`dfm.py`), the dual-method range logic (`ranges.py`) and the per-site orchestrator
(`breach_params.py`) that writes `data/<site_id>/breach/breach_params.json`.

Historical note: the original XZ9 reference-height blocker was resolved with a fixed model
constant `h_r = 15.0 m`. XZ9 breach width and its recommended pair are now computable when their
physical inputs are present. The XZ9 peak-discharge path and its dependent Q_p ranges remain
unavailable. Z20 remains blocked outside HD/CD dam types.

Added `contracts/schemas/breach_params.schema.json` and `contracts/examples/
breach_params.example.json` (generated from `sites/teesta.yaml`, so it shows a real blocked-XZ9
case), wired into `tests/m0_api/test_schemas.py`'s example/schema pairing.

Tests: `tests/m2_breach/` (46 tests) — hand-computed values per equation and branch (F16 O/P,
k_h continuity at h_b=6.1m, a length-scaling dimension check; Z20 HD/CD plus FD/ZD blocked; F8
T_f unit derivation; DFM equals its weighted sum; a blocked component propagates through DFM;
a negative DFM value is flagged (`dfm_nonpositive`), never clipped); `breach_params.py`
end-to-end on both a synthetic config and the real `sites/teesta.yaml` (concrete-dam refusal,
moraine caveat, placeholder propagation, schema validation); and `test_breach_cases.py`, which
reproduces `docs/paper_azmi.md` Table 7's median % error over `tests/data/breach_cases.csv` for
every method that isn't blocked (F16/Z20 for Q_p, F95/F8 for B_ave, F95/F8/MCLM for T_f) — all 7
land within the paper's own reported MAD of its median, with no tuning:

```
F16_Qp:   n=29  our=-11.6%  paper=-10.1%  MAD=31.9
Z20_Qp:   n=41  our= -9.7%  paper=-26.2%  MAD=34.0
F95_Bave: n=128 our= -9.4%  paper= -5.2%  MAD=23.2
F8_Bave:  n=128 our= -6.5%  paper= -1.8%  MAD=29.8
F95_Tf:   n=68  our=  4.2%  paper=-11.3%  MAD=36.6
F8_Tf:    n=68  our=  5.0%  paper= -7.3%  MAD=35.3
MCLM_Tf:  n=68  our= 14.3%  paper=  3.4%  MAD=59.4
```

`pytest -q`: 231 passed (46 new). Ran with `/usr/bin/python3` (this machine's `.venv` is still
missing several packages, per the last session's note).

**Out of scope this session, left for next:** hydrographs (`hydrograph()`, contract §4.2's
`hydrographs/*.csv` + sidecar) and cascades. Also open: sourcing h_r for XZ9, and the FD/ZD
mapping for Z20 — both block real Q_p/B_ave ranges for Teesta until resolved.

## 2026-09-24 — M2 breach hydrographs

Built contract §4.2's hydrograph part, left open at the end of the last session:

- `backend/m2_breach/weir.py`: trapezoidal broad-crested weir discharge (standard hydraulics, not
  from `docs/Equations.md`) — no built-in coefficients, read from config (`docs/decisions.md`).
- `backend/m2_breach/storage.py`: `StorageCurve` above the final breach invert, from a surveyed
  elevation-volume curve or an area-volume-relation derivation (documented, not from a paper).
- `backend/m2_breach/hydrograph.py`: `breach_growth_weir()` (level-pool routed, ODE via
  `scipy.integrate.solve_ivp`, breach width+depth grow together over `failure_time_s`),
  `triangular()` fallback (volume-exact), `hydrograph_for_dam()`/`hydrograph()` (method selection,
  `peak_within_m2_range` against the M2 Q_p range, caveats), `write_hydrograph()`.
- `backend/shared/site_config.py`: additive optional `Dam.volume_elevation` and
  `Dam.breach_hydrograph` blocks.
- `contracts/schemas/hydrograph_sidecar.schema.json` + generated example; registered in
  `tests/m0_api/test_schemas.py`.
- New tests: `tests/m2_breach/{test_weir,test_storage,test_hydrograph}.py` — mass conservation
  (weir and triangular), zero flow before breach start/offset, `peak_within_m2_range` correctness
  (not tuned to force `true` — the M2 Q_p range is blocked for the synthetic dam, so the flag is
  `None`), method fallback/blocking, sidecar schema validation.

`pytest -q`: 280 passed (49 new). Ran with `/usr/bin/python3` (installed `scipy` there via
`pip install --user --break-system-packages`; this machine's `.venv` still has no `pip`).

**Out of scope this session, left for next:** cascades (downstream dam `t_offset_s`), and the
pending site-config-schema decision (`volume_elevation`/`breach_hydrograph` are additive so this
doesn't block, but the wider v1-vs-contract migration is still open).

## 2026-09-24 — M2 cascade engine (multi-dam sites)

Extended M2 for sites with several dams in sequence (Teesta: South Lhonak → Teesta III), left open
at the end of the last session. Contract §3.1's `cascade.approach: null ⚙️` was genuinely
undecided (nothing about it was in this file before today) — confirmed the choice with the user
before building: **`two_stage_imposed`**, not `dambreak_structure`. Full reasoning in
`docs/decisions.md` ("M2 cascade engine: two-stage imposed hydrograph").

- `backend/shared/site_config.py`: additive `Dam.equations_applicable` (default `true`),
  `Dam.imposed_ranges` (`{peak_discharge_m3s, breach_width_m, failure_time_s}`, each a
  `[low, high]` `RangeValue`), `Dam.trigger` (`{type: inflow_threshold, value: DischargeValue}`),
  and top-level `SiteConfig.cascade` (`{approach: two_stage_imposed | dambreak_structure}`). New
  cross-checks: `kind: concrete_dam` ⇒ `equations_applicable: false` ⇒ `imposed_ranges` required;
  `triggered_by` ⇒ site needs a `cascade` block; `two_stage_imposed` ⇒ every triggered dam needs
  its own `trigger`.
- `backend/m2_breach/breach_params.py`: `compute_dam` now branches on `equations_applicable` —
  `false` builds the output ranges from `imposed_ranges` (`interval: "imposed"`, caveat
  `concrete_dam_imposed`) instead of running the Azmi equations. Deleted `DamKindRefused`: a
  concrete dam without `equations_applicable: false` is now rejected by the loader, not by
  `compute_dam`.
- `backend/m2_breach/cascade.py` (new): `cascade_plan()` (ordered stages, raises
  `UnsupportedCascadeApproach` for `dambreak_structure`), `trigger_time()` (linear-interpolated
  threshold crossing on a routed-inflow time series — no routing/celerity invented, the series is
  always an input), `triggered_hydrograph()` (sets `t_offset_s` to the trigger time, or returns
  `None` if the threshold is never reached in that scenario; releases only the dam's own storage —
  superposition, caveat `cascade_superposition`; raises `HydrographBlocked` if the threshold is a
  placeholder).
- `backend/m2_breach/hydrograph.py`: `Hydrograph.trigger` (optional dict), included in the sidecar
  only when set.
- Contracts: `breach_params.schema.json`'s `OutputRange.interval` gains `"imposed"` (nullable
  `selected_pair`, optional `source`); `hydrograph_sidecar.schema.json` gains an optional
  `trigger` object. `contracts/examples/breach_params.example.json` regenerated from
  `sites/teesta.yaml` — no diff (teesta_iii stays an embankment dam today).
- `sites/teesta.yaml`: added `cascade: {approach: two_stage_imposed}` and `teesta_iii.trigger`
  (placeholder — not sourced). `sites/template.yaml`: documented the new fields.
- New tests: `tests/fixtures/m2_breach/synth_cascade.yaml` (synthetic two-dam site: moraine lake →
  concrete dam with imposed ranges and a trigger), `tests/m2_breach/test_cascade.py` (plan
  ordering incl. a three-dam chain, `trigger_time` interpolation, triggered/not-triggered/blocked
  hydrographs, superposition volume check, sidecar schema validation, an end-to-end
  lagged-hydrograph scenario), plus loader cross-check tests in `tests/shared/test_site_config.py`
  and imposed-range tests in `tests/m2_breach/test_breach_params.py`.

`pytest -q`: 307 passed (27 new). Ran with `/usr/bin/python3`.

**Out of scope this session, left for next:** Teesta III's dam type is still unverified
(`docs/ideation.md`) — if it turns out to be concrete rather than embankment, its
`equations_applicable`/`imposed_ranges` need filling from a real source. The per-dam `trigger`
deviation from contract §3.1 is PENDING team agreement (see decisions.md); h_r (XZ9) and the
Z20 FD/ZD mapping are still blocked from earlier sessions.

## 2026-09-25 — M5: synthetic test world (`backend/m5_emulator/synthetic.py`)

`backend/m5_emulator/` was completely empty before this session (no files at all). Built
`docs/m5_specs.md` §7's synthetic test world — the fake-physics stand-in for Delft3D/SPH used to
develop and test the rest of M5 before real runs exist.

- `synthetic_flood_maps(grid, water_volume_m3, breach_width_m, failure_time_s, ...)`: pure,
  deterministic function returning max depth [m] / max velocity [m/s] / arrival time [s since t0]
  numpy arrays on a `CanonicalGrid`, reproducing every qualitative behaviour in §7.2 (gorge depth
  cap, constriction backup with an upstream-biased backwater shoulder, terrace threshold overtop
  with 0.5–1.5 m post-overtop depth, plain attenuation/spreading, arrival delay from failure time
  fading with distance) via smoothstep-blended piecewise geometry — no discontinuities except the
  terrace's deliberately steep (but continuous) sigmoid threshold. `write_synthetic_run()` writes
  the same maps as GeoTIFFs at `summary/{max_depth,max_velocity,arrival_time}.tif`, matching the
  M3/M4 run-result schema (`docs/handoff_contract.md` §4.4) exactly, via `backend.shared.grid.write_grid_raster`.
- **Flagged, not silently changed:** §7.1 sketches non-square cells (e.g. "100 m x 50 m") sized to
  resolve the ~50 m gorge across a valley whose fan is ~2 km wide. `CanonicalGrid` — the grid every
  raster in this project must align to (§1.4) — only supports square cells, and no single square
  size both resolves the narrowest feature (the 12.5 m constriction half-width) and keeps the
  "~40k cells" small-grid target at this valley's full cross width. Resolved by keeping the spec's
  40 km length and picking square-cell grids sized to actually resolve the constriction instead:
  `small_grid()` = 16 m, 2500x200 (500k cells), `large_grid()` = 8 m, 5000x400 (2M cells, same
  order of magnitude as "about 1M" and as the real far-field grids in §1.4's own example).
- Verified monotonicity (acceptance test A3) beyond the unit tests: a 300-sample random sweep
  across the full `DEFAULT_INPUT_RANGES` design space (V_w log-uniform 1e5–1e8, B_ave/T_f uniform
  in their ranges) found **0% non-monotonic pairs** (target: ≤5%) for depth/velocity rising with
  V_w, arrival falling with V_w, and arrival rising with downstream distance, over ~28M wet-cell
  comparisons each. Large grid: 2M cells, one scenario in 0.087 s (A7 target: <2 s).
- Tests: `tests/m5_emulator/test_synthetic.py` (24 tests) — grid presets, output shape/dtype/nodata
  contract, determinism (with and without the noise switch), input validation, monotonic response
  at several points, constriction backup, gorge-capped/plain-wide extent, terrace dry-then-flooded
  threshold, and a GeoTIFF round-trip through `write_synthetic_run` (CRS/transform/nodata/values
  match the in-memory arrays exactly).

`pytest -q`: 331 passed (24 new). Ran with `/usr/bin/python3`.

**Out of scope this session, left for next:** everything else in `docs/m5_specs.md` — scenario
design (LHS over M2's ranges), the run cache, PCA + GP emulator itself, LOOCV, Monte Carlo,
confidence rule, and the empirical fallback. `synthetic.py` only provides the test world those
pieces will be built and tested against.

## 2026-09-25 — m5_emulator: PCA + GP emulator core (fit / predict / save-load / sensitivity)
- `transforms.py`: `log1p` for depth and velocity (docs/m5_specs.md §3 — the pasted task description
  said "log/sqrt"; confirmed with the user to follow the spec's log1p-for-both instead), `identity`
  for arrival; `fill_arrival` replaces dry-cell nodata with `t_end_s` and clips to `[0, t_end_s]`.
- `inputs.py`: `InputScaler` — log10 for `water_volume_m3`, linear for `breach_width_m`/
  `failure_time_s`, standardised to zero mean/unit std on the training design; flags per-input
  extrapolation against the training box (§6 check C, not wired to a confidence rule yet).
- `pca.py`: `corridor_mask` (union of cells wet >0.03 m in any training run, 3-cell 4-connected
  dilation buffer); `fit_pca` — centred thin SVD, smallest D* reaching 99% variance capped at N−2,
  `PCABasis.decode_std` (per-cell `sqrt(sum W_ij^2 sigma_j^2)`, emulator uncertainty only, documented
  as excluding PCA truncation error); components stored float32 per spec.
- `gp.py`: one `GaussianProcessRegressor` per component, `ConstantKernel * Matern(nu=1.5, ARD) +
  WhiteKernel`, length-scale bounds [0.1, 10], `normalize_y=True` (so the spec's noise floor
  "1e-6 x component variance" is the constant 1e-6 in the normalized-target space the optimiser
  actually fits in — documented since it's easy to get backwards), 10 restarts.
- `library.py`: a synthetic-only maximin-LHS training library (§2) — **not** the contract's
  `design/scenario_design.json`; `run_ids` use `model: "synthetic"`, outside the contract's model
  enum, and are only ever written under `tmp_path`. **Bug caught before it shipped:** widening
  `failure_time_s` (300-10800 s) and `breach_width_m` (20-150 m) linearly by 20% of their span drove
  the lower bound negative (`failure_time_s` -> -1800 s), since the synthetic world's illustrative
  ranges span almost an order of magnitude unlike real M2 pair bounds; `water_volume_m3` needed the
  same fix in log space. Fixed: log-space widening for the log10 input, and a positivity clamp
  (`low * 0.5`) for the linear ones.
- `emulator.py` — `FloodEmulator`: `fit` (per output: transform -> corridor-mask -> PCA -> per-
  component GPs, with reconstruction RMSE reported both in transformed space and in physical units
  over Omega/"had a real arrival"); `predict` (GP mean/std -> PCA decode -> inverse-transform,
  depth/velocity clipped >= 0, **arrival explicitly clipped to `[0, t_end_s]`** — caught in testing
  that GP extrapolation can otherwise predict arrivals past `t_end_s`, since `fill_arrival`'s own
  clip only bounds the *training* targets — then arrival masked to nodata outside the predicted wet
  extent, `central depth > arrival_m`); `save`/`load` in the contract §4.6 layout (`manifest.json`,
  `pca_<output>.npz`, `gp_<output>.joblib`, short names `depth`/`velocity`/`arrival`); manifest
  carries extra fields beyond the contract list (`t_end_s`, `grid`, `settings`,
  `reconstruction_rmse.transformed`, `fit_warnings`) needed to reconstruct predictions — flagged as
  additive, nothing renamed/dropped; `sensitivity_table()`/`format_sensitivity_table()` — fitted
  length scales per (output, component) plus a variance-weighted relative-sensitivity summary.
- Verified on the synthetic library (N=30, small grid, 500k cells, seed 42): corridor 117,954 cells
  (23.6%); depth/velocity D*=1 (var. explained 0.996/0.995), arrival D*=10 (0.990); reconstruction
  RMSE (physical) 0.092 m depth, 0.088 m/s velocity, 384 s arrival; fit 3.9 s, single-scenario
  predict 27 ms (spec A7 target < 2 s), save/load round-trip matches to 1e-4. Sensitivity table:
  `water_volume_m3` is the most sensitive input for all three outputs (relative 0.49/0.44/0.63),
  matching the synthetic world's `V_EXP=0.55` > `B_EXP`/`T_EXP` construction.
- Tests: `tests/m5_emulator/{test_transforms,test_inputs,test_pca,test_gp,test_library,
  test_emulator}.py` — 59 new (transforms round-trip/clip/fill; input scaling + extrapolation
  flagging; PCA variance target/cap/reconstruction/decode_std against a brute-force check; GP
  recovers a known function and gives the driving input a shorter length scale than irrelevant
  ones; LHS stratification/maximin/determinism; full fit-predict-save-load-sensitivity integration
  on N=30, including depth beating a training-mean baseline and extent F1 >= 0.8 on 6 held-out
  scenarios). `pytest -q`: 390 passed (59 new).
- Setup: `scikit-learn`/`joblib` installed into the `/usr/bin/python3` user site (conda unavailable
  on this machine, same as the existing `.local` setup); added to `requirements.txt`.

**Out of scope this session, left for next:** LOOCV + `validation/loocv.json`, the full A1-A8
acceptance suite (today's tests check a light subset — a training-mean baseline and one F1
threshold, not the paper's two required baselines or the 90% CI coverage test), Monte Carlo /
unknown-breach mode, the confidence rule, `get_flood()`, the empirical fallback, and the real
`design/scenario_design.json` (today's `library.py` is synthetic-only test scaffolding).

## 2026-09-25 — M5: LOOCV, A1 baselines, validation report, acceptance check

Built the LOOCV pipeline `docs/m5_specs.md` §8 needed and everything before it was still missing:
metrics, both A1 baselines, `validation/loocv.json` (`docs/handoff_contract.md` §4.6) and an honest
A1-A8 check.

- `metrics.py`: one function per metric — extent IoU/F1@{0.05,0.1,0.3}, wet-cell (Omega = depth >
  `wet_m` in truth OR prediction) depth RMSE and velocity MAE, arrival MAE/RMSE (cells where both
  truth and prediction have a real arrival), signed flooded-area % error, 90% interval coverage,
  PCA-projection RMSE (the honest per-fold A5 number, not the in-sample one `pca.py` reports at fit
  time), and terrace majority-vote classification (A4, synthetic-world only).
- `baselines.py`: `LinearScoresBaseline` (OLS per PCA component on standardised inputs, same basis
  as the fold's GP) and `NearestRunBaseline` (IDW power-2 blend of the 3 nearest training runs'
  physical maps, arrival filled before blending). Neither reports an uncertainty interval.
- `emulator.py` refactor (behaviour-preserving, guarded by the existing `test_emulator.py`):
  extracted `FloodEmulator.maps_from_latent()` (PCA-decode -> inverse-transform -> embed -> clip ->
  arrival-mask, used by `predict()` and the linear baseline) and
  `maps_from_corridor_physical()` (embed -> clip -> arrival-mask only, used by the nearest-run
  baseline, which never touches PCA) so every method — GP, both baselines — goes through the exact
  same post-processing pipeline before scoring.
- `synthetic.py`: added `terrace_cells()` (boolean terrace mask for A4) and `SYNTHETIC_POIS` /
  `poi_cell_index()` (5 declared points spanning gorge/constriction/terrace/middle/plain, for A2's
  POI coverage and A3's monotonicity check).
- `loocv.py`: `run_loocv()` — one fold at a time (refits scaler, corridor mask, PCA **and** GPs on
  the other N-1 runs per spec §3; only one fold's maps held in memory at once, CLAUDE.md rule 13),
  scoring the GP and both baselines identically. `build_report()` assembles the contract's
  `loocv.json` shape plus additive fields (**flagged, not silently added** — see
  `docs/decisions.md` "M5 LOOCV: additive validation-report fields", pending team sign-off):
  `baseline_nearest` (contract only has `baseline_linear`), `per_run[].extra`, `acceptance`,
  `settings`/`caveats`/`provenance`/`notes`. `run_acceptance()` checks A1 (GP >=10% better RMSE
  than both baselines, F1 not lower), A2 (90% coverage in [80,95]%, k-factor reported-not-applied
  on failure), A4 (terrace), A5 (PCA projection vs emulator RMSE) from the LOOCV folds;
  `check_monotonicity()` checks A3 separately on the synthetic world directly (300 random V_w
  pairs). A6/A8 (confidence rule) and A7 (Monte Carlo/large-grid performance) are **not
  implemented** this session and report `NOT_EVALUATED`, not a fake pass.
- `validation_plots.py`: 4 PNG charts (`metrics_by_run`, `summary_vs_baselines`,
  `coverage`, `poi_coverage`) — Okabe-Ito colorblind-safe 3-way categorical palette (GP/linear/
  nearest), fixed order, no dual axes, target-band shading for the 80-95% coverage checks. Installed
  `matplotlib` (pip --user --break-system-packages, same constrained setup as scikit-learn last
  session); added to `requirements.txt`.
- CLI: `python -m backend.m5_emulator.loocv --synthetic [--n --seed --grid --out]`. Writes to
  `reports/m5_synthetic/validation/` (new, **gitignored** — not `data/`, since `model: "synthetic"`
  and `m5synth_*` run_ids are outside the contract's ID/model patterns, matching `library.py`'s
  existing rule). `--site <id>` raises `NotImplementedError` (real-run loading is future work).
- Tests: `tests/m5_emulator/{test_metrics,test_baselines,test_loocv}.py` — 55 new (hand-built
  arrays with known metric values incl. edge cases; baselines recover an exact linear function /
  return the exact training map at a training point; a coarse-grid N=10 LOOCV integration suite
  checking fold isolation via a `FloodEmulator.fit` spy, schema validation, grading, acceptance
  table shape, and chart files being written). `pytest -q`: **435 passed** (55 new; `test_emulator.py`
  unchanged and still green, confirming the `predict()` refactor is behaviour-preserving).

**Full run on the synthetic library** (N=30, small grid, seed 42, default settings — 30 folds,
~8 s/fold): `reports/m5_synthetic/validation/loocv.json`, `schema_valid: true` (model substituted
to `delft3d` for the check; the substitution is recorded in the report's own `notes`).

| Test | Result | Detail |
|---|---|---|
| A1 (GP beats both baselines) | **PASS** | depth RMSE 0.067 m (GP) vs 0.139 m (linear) vs 0.243 m (nearest); arrival RMSE 815 s vs 1622 s vs 1466 s; F1@0.3 0.993 vs 0.981 vs 0.976 |
| A2 (90% interval calibration) | **PASS** | wet-cell coverage 87.0%, POI coverage 85.3%, both inside [80,95]% |
| A3 (monotonic response) | **PASS** | 300/300 pairs monotonic on extent, POI depth, and POI arrival |
| A4 (terrace threshold) | **PASS** | 27/30 folds (90.0%) classified the terrace correctly — exactly at the spec's threshold |
| A5 (PCA not the bottleneck) | **FAIL** | median PCA-projection RMSE is NOT <= half the emulator RMSE: depth 0.047 m vs half-of-0.067=0.034 m; arrival 598 s vs half-of-815=407 s — the GP/PCA-decode step is adding more error than the PCA truncation itself, the opposite of what A5 wants |
| A6, A8 (confidence rule) | NOT_EVALUATED | confidence rule (§6) not implemented yet |
| A7 (performance) | NOT_EVALUATED | Monte Carlo / large-grid performance out of scope this session |

**Honest bottom line:** A1-A4 pass; **A5 fails** — reported as-is, no threshold tuning. This says
the GP+kernel/optimizer settings are contributing meaningfully more per-fold error than PCA
truncation does, which is worth the team's attention before real runs (candidates: more GP restarts,
loosening/retuning the Matern length-scale bounds, or accepting current settings and revisiting A5's
threshold). A6-A8 stay unevaluated rather than guessed at.

**Out of scope this session, left for next:** Monte Carlo / unknown-breach mode, the confidence
rule (needed for A6/A8), `get_flood()`, the empirical fallback, the real `design/scenario_design.json`,
and loading real Delft3D/SPH runs for `loocv.py --site`.

## 2026-09-25 — m5_emulator: get_flood(), Monte Carlo, confidence rule
- `backend/m5_emulator/confidence.py`: the S (LOOCV skill) / C (query coverage) / U (prediction
  spread) checks (`docs/m5_specs.md` §6), combined by the count rule in `docs/handoff_contract.md`
  §2.3 (all good → HIGH; one weak → MODERATE; two+ weak, OUTSIDE, empirical fallback, or demo mode →
  LOW), plus the placeholder-input cap. Spec only defines U for depth/arrival and doesn't define
  extent's spread at all; documented deviations: velocity reuses depth's *relative* cutoffs, extent's
  spread reads off the POSSIBLE-vs-HIGH fraction of the flooded area (a draft threshold, flagged as
  such, not sourced). `per_cell_upper_bound()` bounds a GP's response over the whole trained input
  box (via all `2^n_inputs` box corners) without assuming which direction the response grows in —
  used by `monte_carlo.py` to size histogram bins.
- `backend/m5_emulator/monte_carlo.py`: `sample_inputs()` (log-uniform/uniform per input scaling,
  §5.2), `chunk_size_for()` (§5.3's formula — reproduces the spec's own "166 samples/chunk" worked
  example once the 2 GB budget is read as decimal, not `2*1024**3`), and `run_monte_carlo()`: chunked
  GP sampling (`z_j ~ N(mu_j, sigma_j^2)` per component per sample) into fixed-size accumulators —
  exceedance counts, per-cell 64-bin histograms (one `np.bincount` per chunk, not a 64-iteration
  Python loop — the loop was the dominant cost at real corridor sizes) for depth/velocity, and exact
  stored samples for site-wide max depth/velocity, inundated area, and every point of interest
  (depth/velocity/arrival). Full-grid arrival maps are explicitly out of scope (POIs only) — see the
  module docstring for why.
- `backend/m5_emulator/query.py`: `get_flood(emulator, mode, inputs, pois, ...)` — `scenario` mode is
  one GP prediction with an *analytic* P10/P90 band and P(inundation) (`1 - Phi`, no resampling);
  `unknown_breach` mode runs `monte_carlo.run_monte_carlo`. Both produce `p_inundation`,
  HIGH/POSSIBLE `extent_class` (site config `high_p`/`possible_p` thresholds), depth/velocity
  median+P10/P90 maps, arrival median+range at every POI, and per-output confidence.
  `to_contract_response()` assembles a `FloodQueryResponse` dict (`docs/handoff_contract.md` §5.4).
  `peak_discharge_m3s` is honestly reported as a null-valued placeholder Estimate — it's an M2
  output, not one this emulator predicts, and CLAUDE.md rule 3 forbids inventing it.
- Scope decisions made without asking further (all previously flagged to the user before coding):
  `get_flood()` takes an already-fitted `FloodEmulator`, not a `site_id` (no on-disk site→emulator
  loading convention exists yet); unknown-breach mode's default sampling ranges are the emulator's
  own trained design box (`InputSpec.low/high`, already the widened M2 pair bounds per §2), with an
  optional `ranges=` override for a caller that has the real unwidened M2 range; resolving the
  request contract's `{type: exact | slider}` wrappers is left to a future M0/site-config session.
- Found and fixed a pre-existing bug while running the full suite: `test_loocv.py`'s
  `FloodEmulator.fit` monkeypatch restored the *bare* function instead of `classmethod(...)`,
  breaking every `FloodEmulator.fit()` call in test modules that ran after it in the same session.
- Tests: `tests/m5_emulator/{test_confidence,test_monte_carlo,test_query}.py` — 65 new, plus a shared
  `conftest.py` fixture (fits on `synthetic.small_grid()`, matching `test_emulator.py`'s existing
  convention — a coarser custom grid was tried first and rejected because `poi_cell_index`'s
  centreline convention falls outside the corridor mask at coarse resolution in the gorge). Every
  `to_contract_response()` output validates against `flood_query_response.schema.json`. Timing:
  scenario mode on `synthetic.small_grid()` (500k cells) — **~0.03 s**, well under the 3 s ask;
  unknown_breach mode, 2000 samples on the same grid — **~13 s**, under spec's own 60 s target (A7) —
  the 3 s ask is only realistic for a single analytic scenario prediction, not a full per-cell Monte
  Carlo histogram over a ~118k-cell corridor. `pytest -q`: **476 passed** (65 new + 1 pre-existing
  bug fixed; full project suite, no regressions).

**Out of scope this session, left for next:** wiring `get_flood()`/`fallback.py` into
`backend.m0_api` (still fully mocked), A6/A8 acceptance-test wiring in `loocv.py` (the confidence
rule now exists but isn't plugged into `run_acceptance()` yet), and the real
`design/scenario_design.json`.

## Session: M5 empirical fallback (`backend/m5_emulator/fallback.py`)

- Implemented the fallback for sites without a trained emulator (`docs/handoff_contract.md` §4.6:
  `method: "empirical_fallback"`, confidence always LOW): `route_discharge` takes M2's
  `peak_q_m3s` and routes it along the centreline with **no attenuation** (a deliberate,
  documented simplification — conservative rather than an invented decay coefficient);
  `channel_top_width_m`/`channel_roughness` read a per-cross-section active-channel width and
  Manning's n off the HAND/roughness rasters (HAND ≤ 2 m = "in channel", a fallback-only method
  parameter, not a sourced fact); `manning_normal_depth`/`manning_velocity` solve Manning's
  equation for a wide rectangular channel; cells flood where HAND < that station's depth
  (`run_empirical_fallback`); arrival comes from a kinematic-wave celerity `c = (5/3) v`
  (standard open-channel hydraulics, not one of `docs/Equations.md`'s breach equations — cited
  in the docstring instead, per CLAUDE.md rule 4's spirit). `to_contract_response()` mirrors
  `query.py`'s shape with `method: "empirical_fallback"` and the `empirical_fallback` +
  `clear_water` caveats.
- M1 (`backend/m1_terrain`) doesn't exist yet, so `hand.tif`/`roughness.tif`/`chainage_samples.csv`
  don't either (CLAUDE.md rule 2: every module needs synthetic-data tests before real data
  exists). Added `synthetic_fallback_terrain()`: a HAND/roughness/bed-elevation stand-in built
  from the same 40 km valley geometry as `synthetic.py` (§7.1) but **not** through
  `synthetic_flood_maps` — the fallback is tested against terrain alone, never against the
  emulator's own "true" answer for the same scenario.
- Confidence: always LOW with `reason_key: "conf_empirical_fallback"`, via
  `confidence.combine(..., empirical_fallback=True)` — had to pass `query_coverage="INSIDE"`
  (not `"OUTSIDE"`) as the dummy placeholder, since `combine` checks literal `"OUTSIDE"` before
  the `empirical_fallback` flag and would otherwise report the wrong reason (`conf_extrapolation`).
- Contract note: `flood_query_response.schema.json`'s `summary.first_arrival.poi_id`/`.name` are
  non-nullable strings, so `to_contract_response()` needs at least one POI to validate on a wet
  result — added an optional `pois=` param (same `name -> flattened cell index` convention as
  `query.get_flood`). `query.py`'s own `to_contract_response()` has the same latent gap (returns
  `None` when no POI ever arrives); not fixed here, out of scope for this session.
- Tests: `tests/m5_emulator/test_fallback.py` — 18 new, covering each equation in isolation
  (routing is constant with chainage, Manning depth grows with discharge/shrinks with slope,
  celerity floors at a minimum, arrival is monotonic downstream and offsets correctly),
  `run_empirical_fallback`'s flood/dry boundary and discharge monotonicity on the synthetic
  valley, and `to_contract_response()` validating against the schema. `pytest -q`: **494 passed**
  (full project suite, no regressions).

## 2026-09-25 — Timeline (`GET /flood/{query_id}/timeline`, contract §5.5, route #13)

- Contract amendment (user-approved): `timeline.schema.json`/`docs/handoff_contract.md` §5.5
  gained `t_end_s`, `caveats`, `provenance` (required, matching `hydrograph_sidecar.schema.json`'s
  pattern via `common.schema.json`'s `Caveat`/`Provenance` defs); `arrival_profile[].arrival_p10_s`/
  `.arrival_p90_s` may now be `null` ("that percentile never arrives within `t_end_s`"); a chainage
  row is omitted entirely if even the median never arrives. `contracts/examples/timeline.example.json`
  updated to match.
- `monte_carlo.py`: `HISTOGRAM_OUTPUTS` now includes `"arrival_time"` (was POI-only), so
  `unknown_breach` mode gets a full-grid arrival map too. Its bin edges run `[0, t_end_s*64/63)`,
  reserving the top bin for "never arrived" (a sample whose depth never exceeds `arrival_m` is
  recorded as exactly `t_end_s`) — trades that bin's resolution (`t_end_s/63`) for telling "arrives
  very late" from "doesn't arrive" without a second accumulator.
- `query.py`: both modes now gate each arrival band (median/P10/P90) by *that band's own* depth
  (previously scenario mode gated all three arrival bands by the median depth only, hiding arrival
  at POSSIBLE cells outside the median-wet area). `unknown_breach` mode reads arrival percentiles
  off the new histogram, treating a percentile near the top ("never arrived") bin as nodata via a
  half-bin-width tolerance (floating-point-safe, not an exact `== t_end_s` check).
  `to_contract_response()` no longer skips the `arrival_p*` layers for `unknown_breach`.
- New `backend/m5_emulator/timeline.py` (pure numpy + one file-writing entry point):
  `frame_times()` (default 5 min interval, capped at `t_end_s`), `frame_arrays()` (median/HIGH/
  POSSIBLE per contract §5.5's "extent at time t = cells whose arrival <= t"; HIGH gates on P90
  arrival, POSSIBLE on P10 arrival and not-already-HIGH — the two partition the final `extent_class`
  exactly at `t = t_end_s`), `arrival_profile()`, `pois_on_profile()`, `hydrograph_series()` (from
  `m2_breach.Hydrograph`), and `write_timeline_inputs()` (writes `timeline/{arrival_p10,arrival_p50,
  arrival_p90,extent_class}.tif` + `timeline_data.json`, everything a `Timeline` needs except the
  `interval_s`-dependent frame list, which M0 builds per request since the rasters don't change
  with it). `synthetic.py` gained `centreline_samples()` (public wrapper on `_chainage_and_offset`,
  same convention as `poi_cell_index`) standing in for the real, not-yet-built `chainage_samples.csv`.
- New `backend/m0_api/timeline.py`: `find_query_timeline_dir()`, `build_response()` (assembles the
  `Timeline` dict with frame URLs), `render_frame()` (renders one band at one `t_s` via
  `rendering.render_layer_png`, cached beside the source rasters like `render_and_cache`). Wired
  into `main.py`'s route #13 (`interval_s` query param, default 300 s, range [60, 86400], 422 above
  500 frames) and route #22 (`/files/{path}`) via a strict anchored regex
  (`TIMELINE_FRAME_PATH_RE`) matching only `<site>/queries/<query_id>/timeline/{band}_t<t>.png` —
  falls through to the existing mock behaviour for every other path or when no query has been
  written yet.
- Known gap (out of scope this session, flagged in `backend/m5_emulator/__init__.py`): nothing yet
  calls `write_timeline_inputs()` from a live `POST /flood/query` handler (still mocked); real M1
  `centreline.gpkg`/`chainage_samples.csv` don't exist, so real sites still need `synthetic.py`'s
  stand-in replaced once M1 lands.
- Tests: `tests/m5_emulator/test_timeline.py` (12 new — frame monotonicity, HIGH+POSSIBLE
  reproducing `extent_class` at `t_end_s`, arrival-profile ordering/null-handling, hydrograph
  round-trip, both modes' `write_timeline_inputs()` output validating against the schema) and
  `tests/m0_api/test_endpoints.py` (9 new — real timeline route with synthetic inputs written into
  `data_dir`, `interval_s` changing frame count, the 422s, frame PNG rendering/caching, mock
  fallback when nothing's written, and that a malformed `/files/...` path can't escape the regex).
  `pytest -q`: **514 passed** (full project suite, no regressions).

## 2026-09-25 — Compare "emulator vs physics" / "GP vs linear" (contract §5.6, route #15)

- No contract change needed this session: `compare.schema.json`/`.example.json` already had
  `emulator_vs_physics` (held-out LOOCV prediction vs physics, `iou`/`depth_rmse_wet_m`/
  `arrival_mae_s` + diff-map `layers`) and `gp_vs_linear` (aggregate GP-vs-linear-baseline medians)
  fully specified — `backend/m0_api/main.py`'s route just never read real data for them.
  User-confirmed scope: build against the synthetic test world (no real Delft3D/SPH run data
  exists yet, same as every other M5 module so far); precompute the one requested held-out fold's
  diff raster and write it to disk rather than refitting live on every request.
- New `backend/m5_emulator/compare.py`: `emulator_vs_physics_metrics()`/`gp_vs_linear_summary()`
  read `loocv.build_report()`'s per-run GP metrics and baseline aggregate medians back out in the
  Compare contract's shape (no new computation — LOOCV already scores every held-out run against
  both A1 baselines). `fit_and_diff_held_out()` fills the one real gap: LOOCV discards each fold's
  full-grid prediction array after scoring (CLAUDE.md rule 13), so it refits `FloodEmulator` on
  every run but the chosen one via the public `.fit()`/`.predict()` API (not `loocv.py`'s private
  fold internals) and returns `predicted_max_depth - true_max_depth`, reusing the existing
  `depth_diff` diverging style (`contracts/styles.json`) already used by `sph_vs_delft3d.layers` —
  no style/contract change needed for the diff map either. `write_compare_inputs()` writes
  `emulator/<model>/validation/compare/<held_out_run_id>__depth_diff.tif` +
  `<held_out_run_id>.json` (metrics, `gp_vs_linear`, `bounds_latlng`, a `synthetic_world_not_real_physics`
  caveat — same honesty pattern as `loocv.py`'s own report caveat).
- New `backend/m0_api/compare.py`: `find_compare_sidecar()` tries `f"{scenario_id}__{model}"` for
  `model in (delft3d, sph)` under `data/<site>/emulator/<model>/validation/compare/`; `build_response()`
  starts from the mock example (keeping `sph_vs_delft3d`/`when_to_use_key` mocked — no real SPH/
  Delft3D data, out of scope) and overwrites `emulator_vs_physics`/`gp_vs_linear` plus appends the
  sidecar's caveat; `render_diff_layer()` reuses `rendering.render_and_cache`. Wired into `main.py`'s
  route #15 (falls back to the existing full mock when no `scenario_id` is given or no sidecar has
  been written) and route #22 (`/files/{path}`) via a second strict anchored regex
  (`COMPARE_DIFF_PATH_RE`, run_id restricted to id-safe characters), alongside the Timeline one.
- Known gap (flagged in `backend/m5_emulator/__init__.py`, same shape as Timeline's): nothing yet
  calls `compare.write_compare_inputs()` from a live LOOCV run; real M3/M4 run data and a real
  `loocv.json` for an actual site don't exist yet either.
- Tests: `tests/m5_emulator/test_compare.py` (8 new — metrics extraction matches the right
  `per_run` row and returns `None` for an unknown run_id, `gp_vs_linear_summary` shape,
  `fit_and_diff_held_out` provably excludes the held-out run from training (`FloodEmulator.fit`
  spy) and returns a sane-shaped/signed diff array, `write_compare_inputs` output validating
  against `compare.schema.json` once merged into a full Compare-shaped dict) and
  `tests/m0_api/test_endpoints.py` (7 new — real compare route with a small synthetic LOOCV fold
  written into `data_dir`, `sph_vs_delft3d` staying mocked, mock fallback with no `scenario_id` and
  with an unwritten scenario, diff PNG rendering/caching, mock fallback for an unwritten diff PNG).
  `pytest -q`: **527 passed** (full project suite, no regressions).

## 2026-09-25 — M6: exposure download scripts (OSM + WorldPop)

`backend/m6_impact` was completely empty before this session (no files at all). Scope today was
narrow: the `exposure/` *inputs* the contract lists in §4.7 that come from public data (not
`impact.json`, warning tables, loss or exports — those need real M5 output first and are separate
work).

- Dev environment gap found and fixed: `.venv` had no `pip` at all and was missing `httpx`,
  `geopandas`, `shapely`, `fiona`, `scipy`, `scikit-learn`, `fastapi`, `jsonschema` and more,
  despite all being declared in `environment.yml`/`requirements.txt`. Bootstrapped pip
  (`get-pip.py`) and installed the missing packages so `pytest -q` runs clean again.
- New `backend/m6_impact/exposure_osm.py`: queries the public Overpass API for a site's
  `domains.far_field.bbox` and writes `buildings.gpkg` (`building=*`), `roads.gpkg` (`highway=*`),
  `facilities.gpkg` (`amenity=hospital|school` + bridges, one file with a `kind` column per the
  contract's `facilities.gpkg` schema) and `places.gpkg` (`place=city|town|village|hamlet`), each
  with `osm_id`, `kind`, `name` in EPSG:4326. Uses Overpass's `out geom;` so no separate node
  resolution is needed; OSM relations (multipolygon buildings) are NOT fetched — documented
  limitation. `overpass-api.de` returns 406 without a descriptive `User-Agent` and 504 under load
  for large Himalayan-valley bboxes — added a UA header and a retry-with-backoff (5 attempts) for
  502/503/504. Idempotent: an existing layer file is never re-queried.
- New `backend/m6_impact/exposure_worldpop.py`: WorldPop "Global 2000-2020, 1km, UN-adjusted"
  population counts for India (CC BY 4.0). Deliberately NOT the 100m "constrained" product
  (~530 MB and its server ignores HTTP Range requests, so no windowed/partial read is possible)
  — the 1km mosaic is ~18 MB and downloads whole in seconds; population is disaggregated uniformly
  within each ~1km source cell rather than by building footprint, an honest coarse approximation
  noted in `provenance.json`. National raster is cached once under `data/_cache/worldpop/` (shared
  across sites); each site clips it to its bbox into `data/<site_id>/raw/`, then
  `backend/shared/grid.build_farfield_grid` + `resample_to_grid(method="sum")` produce
  `exposure/population.tif` on the far-field grid, sum-preserving by construction (GDAL's `sum`
  resampling area-weights source-to-destination overlap). Idempotent on `population.tif`.
  Both scripts merge their results into one shared `exposure/provenance.json` (dataset, URL/query,
  license, fetched-at, feature/pixel counts) rather than overwriting each other's entries.
- Verified end-to-end for real against `sites/teesta.yaml` (far-field bbox, still all-placeholder
  coordinates per the file's own header — fine for exercising the pipeline, not for real results):
  292,504 buildings, 41,205 roads, 2,387 facilities (2,180 bridges, 140 schools, 67 hospitals),
  279 places, and population resampling that preserved the total exactly (3,099,432.5 persons
  before and after, on the real WorldPop raster). Reran both scripts a second time to confirm every
  file is skipped (no re-download) once present.
- Tests (`tests/m6_impact/`, 23 new, all offline/mocked — no real network calls in CI): Overpass
  element-to-row parsing (buildings/roads/facilities/places tag filtering, relations dropped, short
  ways dropped), `fetch_category` GPKG output shape + skip-existing, provenance merging (including
  that a skip-existing rerun must not clobber the richer provenance entry from the original fetch —
  found and fixed while writing this test); WorldPop URL construction, bbox clipping, sum-preserving
  resample onto a synthetic far-field grid (reusing `tests/fixtures/shared/synth.yaml`), `fetch()`
  skip-existing and placeholder-bbox rejection. `pytest -q`: **550 passed** (full project suite, no
  regressions).
- Not done yet (left for a future M6 session): `hydropower.gpkg` (hand-made, needs a `source`),
  `damage_curves.csv`/`asset_values.csv`, and all of `impact.json` computation / warning table /
  loss / exports — none of today's scripts touch those.

## 2026-09-25 — M6: loss estimation from JRC depth-damage functions

`loss_inr` (`docs/handoff_contract.md` §4.7), scoped to buildings and roads. New:
- `config/impact.yaml`: loss defaults — JRC region/country, depth cap, OSM building→JRC class
  map, and three `SourcedValue`-shaped placeholders (EUR→INR rate, 2010→current price index,
  default road width) that must be filled in before real INR numbers appear.
- `backend/m6_impact/jrc_damage.py`: extracts `damage_curves.csv` (ASIA depth-damage fractions)
  and `asset_values.csv` (India max-damage values, cited by exact sheet/cell) from the JRC
  workbook the user provided (`data/copy_of_global_flood_depth-damage_functions__30102017.xlsx`,
  gitignored — Huizinga et al. 2017, `docs/data_sources.md` src_031/src_032). Verified end to end
  against the real workbook and against `sites/teesta.yaml`'s real OSM exposure data (292,504
  buildings): CSVs generate correctly, cite real cells (e.g. residential India = 212.78 €/m² at
  `'MaxDamage-Residential'!D90`), and `estimate_loss` on the full far-field grid (5016x2534
  cells) completes in ~22s.
- `backend/m6_impact/loss.py`: `damage_fraction` (interpolated JRC curve, capped at 6 m),
  `building_losses`/`road_losses` (footprint-centroid / densified-line depth sampling),
  `estimate_loss` (the top-level `loss_inr` Estimate + `by_asset_class` + `assumptions`). Every
  number that depends on the FX rate, price index or road width comes back as a null Estimate
  while those stay `status: placeholder` — never an invented conversion (CLAUDE.md rule 3).
  Hospitals/schools/bridges (points, no footprint) and agriculture (no cropland layer) are
  explicitly not priced, named in `assumptions` every time.
- Contract change, logged in `docs/decisions.md`: `asset_values.csv` gains additive
  `value_eur2010`/`jrc_cell` columns so every INR figure is re-derivable, not opaque.
- Tests (`tests/m6_impact/test_jrc_damage.py`, `test_loss.py`, 47 new): extraction against a
  mini workbook replicating the real layout plus one test (skipped when the gitignored real
  workbook is absent) that checks the cited cell against the actual JRC figures; damage-fraction
  interpolation/capping; building/road loss pricing (hand-computed expected values, dry/excluded
  buildings, unmapped-kind fallback, placeholder FX/width → null); a stale-CSV staleness check;
  `estimate_loss` P10≤P50≤P90 ordering; and a schema-validation test that splices a real
  `loss_inr` into `contracts/examples/impact.example.json` and validates it against
  `impact.schema.json`. `pytest -q`: **574 passed** (full project suite, no regressions).
- Docs: `docs/impact_outputs.md` §5 "Loss estimation" (every default with its Why, new
  acceptance checks I6-I9); `docs/data_sources.md` (previously empty) now has src_031/src_032;
  `docs/decisions.md` dated entry.
- Not done yet: the EUR→INR rate and price index need a team decision (RBI reference rate;
  CPWD cost index or WPI) before `loss_inr` reports real numbers; road widths by highway class;
  `hydropower.gpkg`; the rest of `impact.json` (`population_persons`, `assets`, `warning_table`,
  exports) — this session only added `loss_inr`.

## 2026-09-25 — M1 kickoff: site config vs contract resolved (0.2.0), M1-1 download.py

Session started to work on `backend/m1_terrain` (found empty, no code, no tests) and found no DEM
files anywhere in the repo or the sites config. Before writing anything DEM-related, resolved the
long-pending `docs/decisions.md` (2026-09-24) "site config schema: YAML v1 vs contract §3.1" split,
per user instruction, then built M1-1.

**Contract 0.2.0** (`docs/decisions.md` 2026-09-25 has the full list): `sites/*.yaml` +
`backend/shared/site_config.py` kept as canonical; `docs/handoff_contract.md` §1.7/§3/§4.1/§4.2/
§5.1/§5.2 rewritten to match, `contract_version` bumped everywhere (doc, `contracts/examples/*`,
`CONTRACT_VERSION` constants in 7 backend modules, FastAPI app version). New `Dam.initial_water_level`
field (was in the 0.1.0 draft, had no home in code). `sites/teesta.yaml`: `crs.utm_epsg` flipped to
`sourced` (it's a derivation from the bbox, not a DEM-dependent lookup); both dams got explicit
`equations_applicable: true` and a `volume_elevation` placeholder block; `teesta_iii` got
`initial_water_level` (placeholder). Generated `contracts/schemas/site_config.schema.json` straight
from `SiteConfig.model_json_schema()` and wired it into `site_create_request.schema.json` — this
caught and fixed a real bug in `backend/m0_api/main.py`'s `create_site` (`site_config.get("site_id")`,
the never-valid 0.1.0-draft key; now reads `site_config["site"]["id"]`). Two known bugs logged but
*not* fixed this session (out of scope): M2 writes a bare `dam_id` instead of the derived
`<site_id>__<slug>` form; M5 builds `poi_id` from a POI's `name` instead of its `id`.
`pytest -q` (excluding `tests/m6_impact`, which needs `geopandas`/`shapely` not installed in this
sandbox — pre-existing environment gap, unrelated): **555 passed**.

**M1-1** (`backend/m1_terrain/download.py`, new module): fetches DEM/landcover *candidates* for a
site's far-field bbox into `data/<site_id>/raw/` — SRTM GL1 and Copernicus GLO-30 via the
OpenTopography Global DEM API (`OPENTOPOGRAPHY_API_KEY` from `.env`), ESA WorldCover 10 m v200
mosaicked from its public S3 COG tiles (no key), and CartoDEM mosaicked from a folder of manually
downloaded tiles (no public bulk API). Deliberately does **not** choose a DEM — that's the M1-2
comparison report, still to come, and the site config has no `dem.source` field for the same
reason. Per-product `provenance.json` (dataset, source id, request bbox, native CRS/resolution/
vertical datum, licence, sha256, fetch time); the API key is never written to provenance or
included in a raised error. 23 new tests (`tests/m1_terrain/test_download.py`), all synthetic —
OpenTopography via `httpx.MockTransport`, WorldCover/CartoDEM mosaicked from small local GeoTIFFs —
no real network calls, no API key spent. Added `docs/data_sources.md` src_033–src_037; **the DOIs
for src_033/src_034/src_036 were written from memory this session and need verifying** before
anything cites them as `status: sourced`.

**Not done yet:** the actual download hasn't been run (no API key spent, per user instruction — the
user runs it). M1-2 (reproject each downloaded DEM onto the canonical grid, build the comparison
report: void % per DEM, pairwise difference maps, valley-centreline elevation profiles, summary
stats) is next, once real DEMs exist in `data/teesta/raw/`. The rest of M1 (`landcover.tif` →
Manning's n, `hand.tif`, `domain_mask.tif`/`domain.gpkg`, `centreline.gpkg`, `chainage_samples.csv`,
`pois.gpkg`, near-field STL) hasn't been started.

## 2026-09-25 — M1 terrain pipeline built end to end

Implemented the rest of `backend/m1_terrain` (M1-1 `download.py` already existed): DEM/landcover
loading + void fill, lake/reservoir extent, dam-crest + reservoir burn-in, flow routing, centreline
+ chainage + POIs, roughness, valley-corridor domain, near-field STL, and the `pipeline.py` that
wires them into `data/<site_id>/terrain/` (`docs/handoff_contract.md` §4.1). Decisions made with
the user before coding, logged in `docs/decisions.md` 2026-09-25 "M1 terrain pipeline: settings,
Manning table path, water extent, flow routing":

- **Manning table**: `config/manning_n.csv` (real column names), not the contract's originally
  documented `data/manning_table.csv` — contract updated to match. Every row is
  `status: placeholder` (Chow 1959 proxies), so `roughness.tif` always sets `has_placeholders`.
- **Lake/reservoir extent**: ESA WorldCover's water class, restricted to the component within
  `snap_radius_m` of each dam's `location`. New output `water_mask.tif`, added to the contract.
- **Flow routing**: our own numpy/heapq priority-flood (Barnes et al. 2014) + an implicit D8
  drainage tree + flow accumulation — `richdem` (CLAUDE.md's Stack) has no wheel for this
  environment's Python 3.12/numpy 2.5. `hydro.py`'s docstring explains the method; HAND is a
  single linear pass over the flood's visit order.
- **Domain**: `TerrainSettings.domain_max_hand_m = 50` (m) default, a CLI-overridable setting, not
  a site fact.
- **DEM product**: `pipeline.py --dem` is required, no default — still the M1-2 comparison
  report's job to choose.

New modules: `settings.py`, `dem.py`, `water.py`, `burn.py`, `hydro.py`, `centreline.py`,
`roughness.py`, `domain.py`, `stl.py` (hand-written binary STL, no `numpy-stl` dependency),
`pipeline.py` (the orchestrator + CLI). `contracts/schemas/` was **not** extended with new schemas
for `grid.json`/`nearfield_frame.json`/`provenance.json` as the original plan suggested —
`contracts/README.md` scopes that directory to API request/response payloads generated from §4/§5,
and these are internal pipeline files documented in §4.1 prose instead; `backend/shared/grid.py`'s
`CanonicalGrid` pydantic model already validates `grid.json`'s shape.

Tests: `tests/m1_terrain/synthetic_valley.py` builds a raw synthetic DEM/landcover pair from
closed-form V-valley geometry (known down-valley/side slopes, a lake and a reservoir bowl of known
radius/depth, a nodata void patch) over `tests/fixtures/shared/synth.yaml`'s bbox; `conftest.py`
adds a second (embankment) dam fixture for reservoir tests. 61 new tests across 9 files (dem,
water+burn, hydro, centreline, roughness, domain, stl, pipeline end-to-end) — all synthetic, no
real DEM needed. Found and fixed one real bug while writing `test_dem.py`:
`rasterio.fill.fillnodata` mutates its `image` argument in place, so `dem.fill_voids` now copies
before calling it. `pytest -q` (full project): **640 passed**, no regressions.

Smoke-tested on real Teesta data (`data/teesta/raw/`, already downloaded): far-field grid is
5016x2534 (30 m), near-field 1011x906 (10 m) — run in the background, see the next session's notes
for runtime/memory and whether the centreline/HAND/domain look sane on the real DEM.

Not done yet: M1-2 (the DEM comparison report that should choose SRTM vs Copernicus vs CartoDEM)
is still unwritten — this session's pipeline just takes `--dem <product>` as given. `sites/
teesta.yaml`'s placeholder dam locations/heights (illustrative, not surveyed) mean a real Teesta
terrain run has `has_placeholders: true` and shouldn't be treated as final.

**Teesta smoke-test results** (real DEM, `--dem copernicus_glo30`): 1m41s wall clock, 1.6 GB peak
RSS, single-threaded — well inside the RTX 4060/16 GB RAM budget, and fine for an offline M1 run.
The centreline (6506 cells, ~239 km total chainage to the grid edge) snapped all 14 POIs in the
**correct real-world downstream order** — Chungthang → Mangan → Teesta V/Dikchu → Singtam → Rangpo
→ Teesta Bazaar → Teesta Low Dam IV → Coronation Bridge → Teesta Barrage — with most
`dist_to_channel_m` in the tens-to-hundreds-of-metres range, a strong sanity check that the D8
routing tracks the real Teesta channel, not just the synthetic valley. South Lhonak's crest burned
in (193 cells raised, 33 cells long); `teesta_iii`'s reservoir wasn't found by the WorldCover
water-mask search (`teesta.yaml`'s dam locations are illustrative placeholders, not surveyed) and
its crest search found no cells needing raising — both expected given placeholder inputs, and both
recorded in `provenance.json` rather than silently skipped.

## 2026-09-25 — M7 GEE fetch built end to end (contract §4.8)

Built `backend/m7_gee/{settings,lake_area,provider,rainfall,recheck,cache,fetch}.py` on top of the
existing `scene_search.py`. Real files now land at `data/<site_id>/gee/`: `lake_area.csv`,
`lake_latest.geojson`, `rainfall.csv`, `gee_meta.json`, `recheck.json`. Out of scope this session:
event imagery PNG/TIF, `observed/*.geojson` (manual digitising), and wiring M0's `GET/POST
/gee/{site_id}` (still mocks) to this cache. Full reasoning in `docs/decisions.md` "M7 GEE fetch".

- Method: Otsu threshold per monthly Sentinel-2 NDWI composite (clamped to a sane range), falling
  back to Sentinel-1 VV when too cloudy, skipped outright when the SCL snow/ice class says the lake
  is frozen. A connected-components labelling seeded at the dam's `location` keeps only the lake's
  own component, dropping SAR shadow and unrelated water in the AOI buffer.
  `lake_area.otsu_threshold`'s tie-break (middle of a run of equal-variance thresholds, not the
  first) matters in practice: a well-separated bimodal histogram has many empty bins between the
  clusters, and picking the first tied index put the threshold at the edge of the gap instead of
  its centre.
- Rainfall catchment: HydroBASINS level 12 (`WWF/HydroSHEDS/v1/Basins/hybas_12`), walked upstream
  via `NEXT_DOWN` (`provider.walk_upstream_basin_ids`) — no basin polygon exists anywhere else in
  the repo. Its own unit test (`tests/m7_gee/test_provider.py`) caught a real bug: a diamond-shaped
  drainage graph (two basins both draining into a third further upstream) produced a duplicate ID
  in the walk, because only cross-batch de-duplication was checked, not within-batch. Fixed with
  `dict.fromkeys`.
- `recheck.json`'s reference area comes from the trained emulator's `manifest.json` `trained_at`
  (`backend/m5_emulator/emulator.py`), not an arbitrary baseline; an untrained site reports
  `outdated: false, reason: "no_trained_library"` rather than guessing.
- Widened `contracts/schemas/gee_layers.schema.json`'s `recheck.change_pct` to allow `null`
  (additive, documented in decisions.md) so `cache.load_layers()` can represent "no trained library
  yet" honestly instead of inventing a 0.
- Added `docs/data_sources.md` src_038–041 (Sentinel-2 SR, Sentinel-1 GRD, CHIRPS, HydroBASINS),
  DOIs verified by web search this session (CHIRPS: Funk et al. 2015, doi 10.1038/sdata.2015.66;
  HydroBASINS: Lehner & Grill 2013, doi 10.1002/hyp.9740), same "verify before `status: sourced`"
  caveat as src_033/034 for the ones with a real DOI.
- Tests: 73 in `tests/m7_gee/` (up from 8), all synthetic (CLAUDE.md rule 2) — a
  `provider.SyntheticProvider` (shrinking disc lake, cloudy/icy/missing months, flat rainfall)
  drives `fetch.run()` end to end. `EarthEngineProvider`'s actual `computePixels`/HydroBASINS/CHIRPS
  calls are untested beyond request-shape checks; **not yet run against live Earth Engine** — the
  next session (or whoever has EE credentials) should run `python -m backend.m7_gee.fetch teesta
  --months 24 --ee-project <proj>` and sanity-check the output against the known Oct 2023 South
  Lhonak drainage (lake area should drop sharply across that month).
- Full `pytest -q`: 713 passed (640 before this session), nothing else broken.

## 2026-09-25 — M7 GEE event imagery + wiring the real `/gee` endpoints

Picked up the "out of scope" leftovers named at the top of the previous M7 session: event
imagery, `observed/*.geojson`, and wiring `backend/m0_api/main.py`'s real `GET/POST /gee`. Dropped
observed-extent work this session (`data/teesta/observed/flood_extent_2023.geojson` doesn't exist
yet — confirmed with the user, revisit once the digitized file exists).

- **New `backend/m7_gee/imagery.py`**: converts the pre-/post-event RGB GeoTIFFs an operator has
  already staged (`sites/<site_id>.yaml` `events[].imagery_pre_event/imagery_post_event.source`,
  e.g. `cache/gee/teesta/teesta_pre_event.tif` → its `..._rgb.tif` sibling) into
  `data/<site_id>/gee/imagery/`: full-res PNG, a ≤512px fallback PNG (`_fallback.png`, for slow
  connections — a *different* concept from contract §4.8's `fallback/*.png` "screenshots when live
  and cache both fail", deliberately kept in a separate location/naming so it doesn't trip
  `cache.fallback_screenshots()`'s `source: screenshot_fallback` logic), and a `manifest.json`
  recording each PNG's EPSG:4326 bounds. `cache.read_imagery()`/`load_layers()` read the manifest
  into `GeeLayers.imagery` (contract §5.8; `fallback_url` added per entry — the schema doesn't
  constrain `imagery`'s item shape, so this is additive, not a contract change).
  - Ran it for real against `cache/gee/teesta/*_rgb.tif`: wrote
    `data/teesta/gee/imagery/sikkim_glof_2023_{pre,post}_2023{0928,1006}{,_fallback}.png` +
    manifest (event id/dates from `sites/teesta.yaml` — `sikkim_glof_2023`, not the
    `teesta_2023` placeholder still in `contracts/examples/gee_layers.example.json`).
- **New `backend/m7_gee/live_render.py`**: best-effort live re-render of the same pre-/post-event
  composite from Sentinel-2 (least-cloudy scene within ±15 days of the event date, near-field AOI),
  overwriting the staged `_rgb.tif`. Called by `imagery.refresh()`; any failure (no credentials, no
  network, no usable scene) is caught and the existing cached PNGs are kept — same fallback
  contract as `fetch.run()`. **Untested against real Earth Engine** — no service-account key exists
  yet (see below); like `provider.EarthEngineProvider`, only its request-shape is implicitly
  exercised via the type signature, not a real `computePixels` call.
- **`scene_search._ee_initialize`** now also accepts a service account: `GEE_SERVICE_ACCOUNT_EMAIL`
  / `GEE_SERVICE_ACCOUNT_KEY_PATH` from the environment or repo `.env` (same lookup pattern as
  `m1_terrain.download.opentopography_api_key` — never logs the key file's contents, only its
  path). Falls back to the existing `ee.Initialize(project=...)` flow when neither is set. Neither
  var is in `.env` yet — wired up ahead of the key existing, per user decision this session.
- **`backend/m0_api/main.py`**: `GET /gee/{site_id}` now serves `gee_cache.load_layers()` once
  `gee_meta.json` exists for the site (i.e., `fetch.run` has actually run at least once);
  otherwise still falls back to the contract mock, same as every other not-yet-real endpoint.
  `POST /gee/{site_id}/refresh` tries `_ee_initialize` + `EarthEngineProvider`, falls back to
  `gee_fetch._CacheOnlyProvider` on any init failure, runs `gee_fetch.run` + `gee_imagery.refresh`,
  and reports `source: live` only if both actually succeeded live — never 500s over a live-fetch
  problem (only over `site_id` not being configured). Added `GEE_IMAGERY_PATH_RE` to `GET
  /files/{path}` to serve the new `gee/imagery/*.png` files for real, 404ing by name if a specific
  file is missing (never silently falling through to a mock PNG for a path that matches this
  pattern).
  - Ran `fetch.run("teesta")` once for real (no live EE — fell back to cache as designed) so
    `gee_meta.json` exists and `GET /gee/teesta` now serves the real imagery end to end; verified
    over HTTP with `uvicorn` (`curl .../api/v1/gee/teesta`, `curl .../files/teesta/gee/imagery/....png`
    → 200 image/png; a nonexistent filename → 404 naming the exact path).
- Treated the pasted `?refresh=true` request as the contract's existing `POST
  /gee/{site_id}/refresh` (§5 row 20) rather than adding a query param to `GET` — same behaviour,
  already on record; flagged to the user rather than silently deviating either way.
- Tests: new `tests/m7_gee/test_imagery.py` (9 tests, synthetic GeoTIFFs, no EE), plus
  `TestEeInitialize` in `test_scene_search.py` (3 tests for the service-account credential path,
  `fake_ee.FakeEE` extended with `ServiceAccountCredentials`/`Initialize(credentials=...)`), plus
  `cache.py` imagery-wiring tests. Full `pytest -q`: 727 passed (713 before this session).

## 2026-09-25 — M7: real Teesta imagery re-export, scene IDs, observed-extent loader

User exported real pre-/post-event Sentinel-2 GeoTIFFs for Teesta from Earth Engine into
`cache/gee/teesta/` (`COPERNICUS/S2_SR_HARMONIZED/20230926T.../20231026T...`, near-field AOI,
EPSG:32645, 10 m) and asked to wire them through, plus load a hand-digitized flood outline as the
observed-extent layer. See `docs/decisions.md` "M7: recording scene IDs, observed-extent loader"
for the scene-ID-placement decision and the observed-extent scope decision, both made with the
user before coding.

- **`sites/teesta.yaml`**: `events[0].imagery_pre_event/post_event.value` corrected to the real
  scene acquisition dates (`2023-09-26`/`2023-10-26`; were `2023-09-28`/`2023-10-06`, an earlier
  arbitrary pick). `source` unchanged (already pointed at the right `cache/gee/teesta/
  teesta_{pre,post}_event.tif` files).
- **`backend/m7_gee/imagery.py`**: `convert()` now takes optional `scene_ids={"pre": [...],
  "post": [...]}` and merges an `"imagery"` entry into `gee_meta.json` (dataset, scene_ids,
  acquisition_dates, source) — the contract's per-product meta entry for imagery had never
  actually been written before (only `lake_area`/`lake_latest`/`rainfall` were, both this session
  and last). New CLI flags `--pre-scene-id`/`--post-scene-id` (repeatable).
  - Ran for real: `python -m backend.m7_gee.imagery teesta --pre-scene-id
    COPERNICUS/S2_SR_HARMONIZED/20230926T043709_20230926T045046_T45RXL --post-scene-id
    COPERNICUS/S2_SR_HARMONIZED/20231026T043849_20231026T044734_T45RXL`. Wrote
    `sikkim_glof_2023_{pre,post}_202309{26},202310{26}{,_fallback}.png` + manifest +
    `gee_meta.json` `imagery` entry; deleted the now-orphaned `_20230928`/`_20231006` PNGs from the
    earlier arbitrary dates. Verified `cache.load_layers("teesta")` end to end (source: cache,
    imagery URLs point at the new files, validates against `gee_layers.schema.json`).
- **New `backend/m7_gee/observed.py`**: `convert(site_id, event_id, source_geojson,
  digitized_by, method=..., imagery_ref=..., date=...)` stamps contract §4.8's required properties
  (`event_id, method, imagery_ref, digitized_by, date, kind: observed`) onto an operator-supplied
  GeoJSON and writes `data/<site_id>/gee/observed/<event_id>_observed.geojson`.
  `cache.read_observed_extents()` (new) reads every file in `gee/observed/` into
  `GeeLayers.observed_extents` (was hardcoded `[]` in `load_layers()`). `backend/m0_api/main.py`
  `GET /files/{path}` now serves `gee/observed/*_observed.geojson` for real
  (`GEE_OBSERVED_PATH_RE`, same anchored-regex pattern as the imagery PNG route).
  - **Not run against real data**: `data/teesta/observed/flood_extent_2023.geojson` (the
    hand-digitized outline described this session) does not exist on disk yet — same blocker as
    last session. Built and tested against a synthetic GeoJSON fixture only, per the user's
    explicit choice. Once the file exists: `python -m backend.m7_gee.observed teesta
    sikkim_glof_2023 data/teesta/observed/flood_extent_2023.geojson --digitized-by <name>`.
- Tests: new `tests/m7_gee/test_observed.py` (9 tests), 3 new cases in `test_imagery.py`
  (scene-ID → `gee_meta.json` merge), 2 new cases in `test_cache.py`
  (`read_observed_extents`/`load_layers` wiring). **Could not run the full `pytest -q`** — this
  session's shell has no active conda env; system `python3` lacks `shapely`, so `lake_area.py`
  (imported by `fetch.py`, imported by `m0_api.main`) fails to import, breaking collection of
  `test_fetch.py`, `test_lake_area.py`, `test_provider.py`, and all of `tests/m0_api`. Ran what
  could run: `tests/m7_gee/test_imagery.py`, `test_observed.py`, `test_cache.py` — all pass (34
  tests). Manually verified `GeeLayers` schema validation and the `GET /gee/teesta` code path by
  calling `cache.load_layers()` directly. **Next session (with the real `sih26` env): run full
  `pytest -q` to confirm nothing broke.**

## 2026-09-25 — M0: scheduled site re-checks (lake-area + library-age)

Summarised existing `backend/m0_api` state first: the job system (`registry`/`jobs`/`worker`) is
real, but `GET /sites`, `PUT /sites/{id}/recheck` and `POST /sites/{id}/rerun` were pure mocks —
no persistence, and nothing ever created a `recheck` job even though `jobs.STAGES["recheck"]`
already existed. Found three real, unresolved design questions before writing anything (registry
§4.5's frozen table list has no `sites` table; docs/decisions.md open question #4 about a lapsed
re-check; `rerun` has no job stages defined) and got the user's decisions on all three — logged in
`docs/decisions.md` ("M0 scheduled site re-checks" this session).

Implemented:
- **`backend/m0_api/registry.py`**: `utc_now_dt()` — the one "now" the whole module now goes
  through (`utc_now()` formats it), so a fake-clock test only needs to monkeypatch one function.
- **New `backend/m0_api/site_status.py`**: `data/<site_id>/site_status.json` — `frequency_days`
  (default 90, contract §5.1's own example), `lake_area_change_threshold_pct` (default 10, matches
  `GeeSettings.recheck_threshold_pct`), `last_checked_at`/`next_check_at`, `outdated`,
  `status_reason_key`, `status_detail`. `set_frequency()` (recomputes `next_check_at` from the
  last check, not from now), `record_check()` (a completed re-check's result), `is_due()`,
  `overlay()` (patches a mocked `SiteSummary` with the real state).
- **`backend/m7_gee/fetch.py`**: extracted `best_effort_provider()` — try live Earth Engine, fall
  back to `_CacheOnlyProvider` — from what `POST /gee/{id}/refresh` did inline; now shared with
  the worker.
- **`backend/m0_api/worker.py`**: every `tick()`, `_schedule_rechecks()` queues a `recheck` job for
  any known site with a published library (`emulator/<model>/manifest.json` exists) and no active
  job, once its `site_status.json` schedule is due (or has never run) — no separate timer. The
  `checking` stage is now real for `kind == "recheck"` (`_run_recheck`): M7's lake-area check via
  `gee_fetch.run()`, reading back `gee/recheck.json`; if that's not outdated, a library-age check
  against `site_status.DEFAULT_MAX_LIBRARY_AGE_DAYS` (365 days, new engineering knob). Either
  outcome calls `site_status.record_check(...)`, which always advances the schedule.
- **`backend/m0_api/main.py`**: `GET /sites`/`GET /sites/{id}` overlay `site_status.overlay()` onto
  the mocked base response (same pattern as `_gee_layers_or_mock`); `PUT /sites/{id}/recheck` now
  really persists via `site_status.set_frequency()`; `POST /sites/{id}/rerun` now really queues a
  job — but an `onboarding`-kind one, honestly documented as not yet reusing terrain (that needs
  the still-undecided `rerun` stage list).
- Tests: new `tests/m0_api/test_site_status.py` (9 cases, fake clock via monkeypatching
  `registry.utc_now_dt`), new `tests/m0_api/test_recheck_scheduling.py` (9 cases: no job without a
  library, job queued when due, none when not due, no duplicate while one is active, lake-area vs.
  library-age triggers and their priority, and the "lapsed schedule alone never flags outdated"
  case from open question #4), plus new/extended cases in `tests/m0_api/test_endpoints.py` for the
  real `GET /sites`, `PUT /sites/{id}/recheck` and `POST /sites/{id}/rerun` behaviour.
- **Test environment**: installed `shapely` via `pip install --user --break-system-packages` (no
  `sih26` conda env available in this shell) so `tests/m0_api` and `tests/m7_gee` could collect at
  all. Ran the full `pytest -q` (minus `tests/m1_terrain`/`tests/m6_impact`, which need
  `geopandas`, not installed here — pre-existing gap, unrelated). 652 passed, 1 pre-existing
  failure: `tests/m0_api/test_endpoints.py::test_get_file_geojson` — confirmed via `git stash`
  that it already failed before this session's changes (the file it requests,
  `data/teesta/gee/observed/teesta_2023_observed.geojson`, was never written to disk; last
  session's own notes already flag `data/teesta/observed/flood_extent_2023.geojson` as missing).
  Left untouched — out of scope for this session, not introduced by it.

**Next session**: decide and implement real `rerun` job stages if "re-run reuses terrain" needs to
actually skip terrain; write `data/teesta/observed/flood_extent_2023.geojson` (or accept the
mismatch and fix `test_get_file_geojson`'s fixture) to clear the one remaining failing test.

## 2026-09-25 — M4: DualSPHysics near-field case generator (`backend/m4_sph/`)

Found `backend/m4_pilot/` holds only calibration logs (`vram_estimator.py` + three log files),
not a template case as the session brief assumed — the logs turned out to be from DualSPHysics
5.4.3's own stock `01_DamBreak/CaseDambreakVal2D` example, and the real install (with Linux
binaries) is at `/mnt/d/APPS/DualSPHysics_v5.4/` on this machine, not in the repo. Decisions
(inlet flow source, settings location, inlet geometry) recorded in `docs/decisions.md`
"M4 pilot case", 2026-09-25.

Built, in order (one commit each):
- `backend/m4_sph/case_xml.py`: a GenCase `_Def.xml` writer (constants, geometry, draw commands,
  inlet/outlet zones, gauges, parameters) plus `canonicalize()`/`diff_trees()` for comparing
  against a reference file. `generator.pilot_case_spec()` reproduces the pilot's calibration case
  and regenerating it matches the real `CaseDambreakVal2D_Def.xml` exactly
  (`tests/m4_sph/test_pilot_regen.py`).
- `backend/m4_sph/settings.py` + `config/m4_sph.yaml`: SPH numerical/solver settings
  (dp, time window, inlet size, VRAM budget), project-maintained, not a site fact.
- `generator.hydrograph_to_velocity()`: converts an M2 discharge hydrograph to a uniform inlet
  velocity over a fixed cross-section, mass-flux conserving.
- `backend/shared/probes.py` + `generator.build_nearfield_case()`/`write_case()`: ties together
  M1 terrain (`nearfield.stl`, `dem_nearfield.tif`, `centreline.gpkg`, `pois.gpkg`) and an M2
  hydrograph into a near-field GenCase, with the inlet placed at
  `domains.near_field.inflow.location` and oriented along the local channel tangent. Verified
  end-to-end against a synthetic V-shaped valley (real M1 pipeline output). `far_field` inflow
  (Teesta today) raises `InflowUnavailable` until M3 exists.
- `tests/m4_sph/test_gencase_smoke.py` (skipped unless `DSPH_BIN_DIR` is set): actually runs the
  real GenCase binary on both the pilot case and a generated 3D near-field case. This caught two
  bugs schema validation alone wouldn't have — `hswl` must be set explicitly (not `auto`) because
  the case starts with zero fluid particles, and boundary `mk` values must stay inside
  `mkconfig`'s declared `boundcount` — both fixed.

**Known gap, not yet verified**: whether the inlet's `rotateaxis` angle sign convention actually
points the imposed flow downstream (vs. upstream) needs visual inspection of a generated case in
a VTK viewer — out of scope this session; flagged in `InletGeometry`'s docstring and
`docs/decisions.md`.

**Out of scope this session** (per the approved plan): launching the solver as a job, post-
processing to `summary_nearfield/*.tif`/`timeseries.csv`/`surfaces/*.glb`, `run_meta.json`,
initial reservoir water, far-field inflow from M3, and `snap_pois`'s wrong return type
annotation.

**Next session**: visually verify the inlet rotation direction in a VTK viewer against a real
GenCase output; then M3 (Delft3D 4 FLOW) or M4 post-processing/job wiring.

## 2026-09-26 — D-Flow FM kernel built in WSL (M3 prep, before M3-B housekeeping)

- **Kernel works:** D-Flow FM 1.2.184 + DIMR 2.00 from the Deltares DIMRset 2026.01 source tarball,
  built unmodified with Intel oneAPI 2024.2 (ifort) + Intel MPI 2021.13. netcdf-fortran 4.6.1 and
  PETSc 3.21.3 were built from source with Intel. Install tree: `~/delft3d/dflowfm-2026.01/lnx64`.
  Full record, every command, and problems/fixes: `docs/dflowfm_kernel_build.md`.
- **Verified runs:** release example `01_dflowfm_sequential` (via `run_dimr.sh`) and D-Flow FM
  tutorial06 (Western Scheldt, 10 days, 3.5 min, 0 errors, `_map.nc` + `_his.nc`). The 2015 tutorial input
  needed 3 run-copy-only fixes (obsolete MDU keywords; `Discharge.bc` not covering t0..TStop).
- **Python tools:** hydrolib-core 1.4.0 / meshkernel 8.3.0 / dfm_tools 0.47.0 in a separate venv
  (`~/delft3d/fm-py-venv`). A hydrolib-written case ran on the kernel and dfm_tools read its output.
  For M3: hydrolib writes `.ext` v3.00, which the kernel ignores, so set `fileversion = "2.01"`. The run
  scripts exit 0 even when the kernel rejects the input, so M0/M3 must check the `.dia` and outputs.
- **Tried and dropped:** GNU (gfortran 13 + OpenMPI). ≥28 errors from Intel Fortran extensions; no
  patched Deltares source was kept.
- **Not done, on purpose:** `CLAUDE.md`, `docs/decisions.md` and `environment.yml` are unchanged
  (M3-B housekeeping). flow2d3d wasn't built (estimate in the build doc). `.wslconfig` is only
  proposed. No repo code changed and no tests added this session, so `pytest` wasn't rerun.

## 2026-09-25 — M4: post-processing (`summary_nearfield/`, `surfaces/`, `timeseries.csv`, `run_meta.json`)

Before starting: the user moved `backend/m4_pilot/`'s `vram_estimator.py` and its three
calibration logs straight into `backend/m4_sph/` (consolidating the pilot-calibration-only
folder into the real module). Fixed the fallout — `generator.py`'s import, `tests/m4_pilot/` ->
`tests/m4_sph/test_vram_estimator.py`, `CLAUDE.md` — all green before starting new work
(`docs/decisions.md` today's entry has the detail).

Added the rest of contract §4.4's M4 outputs, turning a completed near-field run into the same
schema a Delft3D run would produce:

- `backend/m4_sph/measuretool.py`: wraps `MeasureTool_linux64` for `summary_nearfield/`'s three
  rasters — column-collapsing `-elevation` search for depth/arrival, explicit multi-level points
  for depth-averaged velocity (`docs/decisions.md` "SPH velocity: depth-average, not a
  fixed-height point"). Every format detail (points-file syntax, CSV layout, the `-elevation`
  column-collapse threshold, `-kcdummy`'s actual no-op behaviour) was checked against the real
  binary on real pilot particle data, not assumed from `-h` text.
- `backend/m4_sph/gauges.py`: `timeseries.csv` from the solver's own `GaugesSWL_*.csv`/
  `GaugesVel_*.csv` (real-time gauges `generator.py` already places per probe) — no MeasureTool
  involved. Fixed a real bug this surfaced: `case_xml.py`'s `VelocityGauge` wrote `<vel>` instead
  of the real `<velocity>` tag, which would have silently produced no gauge output at all.
- `backend/m4_sph/vtk_polydata.py` + `gltf_writer.py`: a from-scratch legacy-VTK-binary reader and
  minimal `.glb` writer (no new dependency) for `surfaces/t<seconds>.glb`, converting
  `IsoSurface_linux64 -saveiso` output at `settings.surface_interval_s` (default 300 s) cadence.
- `backend/m4_sph/postprocess.py`: orchestrates all of the above into `run_meta.json` too, chunked
  by near-field grid row (`settings.postprocess_row_chunk`, CLAUDE.md rule 13). New caveats
  `sph_arrival_below_resolution` (always, since SPH particle spacing is far coarser than the
  0.1 m arrival threshold) and `sph_depth_search_capped` (if a cell's depth nears the search
  ceiling); added both plus `fixed_area_inlet` to the contract's standard caveat list (§2.4).
  `contracts/schemas/run_meta.schema.json` + example added (didn't exist before).
- Settings: `config/m4_sph.yaml` gained `elevation_dz_dp_fraction`, `velocity_levels`,
  `surface_interval_s`, `postprocess_row_chunk`. `elevation_dz_dp_fraction` (not a fixed metre
  value) exists because of a real bug caught mid-session: a flat `elevation_dz_m` silently broke
  MeasureTool's column collapsing on a coarser case (one output column per candidate instead of
  one per cell) — scaling the step with the case's own `dp_m` fixed it for any case, not just this
  one (`docs/decisions.md` has the full story).
- Every new module has offline unit tests (literal CSV/VTK fixtures) plus a real-binary
  integration test gated on `DSPH_BIN_DIR`, run against the shipped pilot dam-break particle data
  (already on disk — no solver run needed). All pass both with and without `DSPH_BIN_DIR` set.
- **Approved by the user this session, not yet implemented**: `/compare`'s `sph_vs_delft3d`
  section (still the all-zero mock), `velocity_diff`/`arrival_diff` styles, and making the mock's
  `available: true` become `available: false` when no run exists yet.

Full `m4_sph` suite: 70 passed with `DSPH_BIN_DIR` set (real binaries), 64 passed / 6 skipped
without it. Full repo suite green.

## 2026-09-25 — M3 Phase 0a/0b: entries lost in doc reorganization

This session's progress-log entries for "M3 Phase 0a: Delft3D 4 FLOW decided, `base_flow` schema
addition" and "M3 Phase 0b: pilot inputs, scenario, and GUI-build recipe" were destroyed on
2026-09-26 when an earlier reconstruction step overwrote this file without a backup. The
decision itself is intact and unaffected: `docs/decisions.md` "M3: Delft3D 4 FLOW, not FM" (now
marked SUPERSEDED) and the `inflow.base_flow` schema addition in `docs/handoff_contract.md`.
What's lost is only the narrative session-log detail (what was tried, in what order, any dead
ends) — not any decision, schema, or code.

## 2026-09-26 — simulation.sph.scenarios, real M5 scenario design, campaign.py (reconstructed entry)

This entry documents code already merged in three commits (`00211bc`, `6c82f14`, `cf4d209`); its
original progress-log prose was lost in the same doc-reorganization overwrite as the M3 Phase
0a/0b entries above. Reconstructed from the commits' own messages/diffs (`git show`), not from
memory — labeled as a reconstruction, not a session-log entry.

- **`00211bc` — `site_config`: add `simulation.sph.scenarios`.** Only the `sph.scenarios` piece of
  the `simulation` block `docs/handoff_contract.md` flagged as missing from `SiteConfig` (contract
  §4.4: which of a site's scenarios also get a near-field DualSPHysics run). `delft3d` settings stay
  deferred (`backend/m3_delft3d` didn't exist yet at the time); SPH solver knobs stay in
  `config/m4_sph.yaml`, not per-site, per that module's existing "not a site fact" decision.
- **`6c82f14` — `m5_emulator`: real `scenario_design.json` generator.** `backend/m5_emulator/
  scenario_design.py`: a maximin-LHS training design over M2's computed `breach_width_m`/
  `failure_time_s` pair ranges (`docs/m5_specs.md` §2), writing the real `design/
  scenario_design.json` the contract defines (§4.3) — `library.py`'s design was synthetic-test
  scaffolding only and never wrote this file. `water_volume_m3` stays fixed at the site config's
  own value (M2 gives no range for it — a physical input to M2's equations, not a ranged output).
  Raises `ScenarioDesignBlockedError` instead of guessing when a required range or fixed physical
  input is blocked. The previous XZ9 reference-height blocker on `breach_width_m` is resolved.
- **`cf4d209` — `campaign.py`: build + register SPH near-field cases, no launcher yet.**
  `backend/campaign.py`'s `run_sph_campaign()`: for every `scenario_id` in a site's
  `simulation.sph.scenarios`, builds and validates its near-field GenCase case
  (`m4_sph.generator.build_nearfield_case`, which already estimates VRAM and raises
  `OverVramBudget` when no feasible `dp_m` fits the 8 GB budget — CLAUDE.md rule 13), then
  registers a `queued` `runs` row under one shared campaign job (`backend.m0_api.jobs`, contract
  §4.5). A scenario missing from the design file, over budget, or with no usable inflow yet is
  reported `"refused"`, not silently dropped. Scoped to SPH only, and to build+register, not
  launch: no DualSPHysics/GenCase launcher exists yet, and `backend.m0_api.worker`'s `simulating`
  stage is still fake ("M1, M2 and M5 plug in here later").

**Net effect:** the schema field + M5 generator + campaign.py are real and tested, but the actual
ask (queue and run Teesta's SPH scenarios) was blocked at the very first step — no real scenario
design exists for Teesta until `XZ9`'s `h_r` is sourced, so `simulation.sph.scenarios` stays empty
and nothing is queued yet.

## 2026-09-27 — M3 D-Flow FM pilot, specification, and generator

- **Built Phase 0 pilot:** `backend/m3_pilot/dflowfm/build_teesta_pilot_s001__dflowfm.py` writes a relocatable
  D-Flow FM case from the M3 common pilot loaders. It uses a MeshKernel mesh, positive-up DEM
  bed levels, Manning samples, 2-hour base-flow spin-up, breach point source, downstream Neumann
  boundary, named POIs, and lean map/history output. The 30-hour run met the M3 success rule
  (no `** ERROR` in `.dia`, `_map.nc` and `_his.nc` present). It has 33,018 faces, ran in 475.09 s,
  and produced a 481,977,476-byte map. The compact metrics and quick map are kept in `backend/m3_pilot/dflowfm/`;
  large NetCDFs and per-face CSVs stay local and are ignored by Git.
- **Specified Phase 1:** `docs/m3_spec.md` records the six M3 rules, pilot choices, kernel and
  file-format versions, CRS, and provisional reproduction tolerances. `sites/template.yaml` now
  declares optional `domains.far_field.inflow.base_flow`; Teesta's value remains null/placeholder.
  The Phase 2 generator requires a non-null configured value. Pilot hydrograph exports already
  include base flow; M2 hydrographs do not, so the generator adds configured base flow once.
- **Built Phase 2:** `backend/m3_dflowfm/generator.py` builds FM cases from site config, M1 terrain,
  and M2 scenario hydrographs; checks mesh round-trip and quality; writes `.ext` version 2.01,
  relative paths and lean output; strips `/mnt/*` from `PATH` for detached kernel runs; and exposes
  an M3 `.dia` plus NetCDF success check. Synthetic-site tests pass (3 passed).
- **Phase 3 was run but not accepted:** `docs/m3_reproduction.md` records the comparisons. The
  generated mesh has 3,234 faces vs the pilot's 33,018; all four POIs are dry, and maximum depth is
  1,841.44 m vs 65.23 m. The generated kernel run formally succeeded, but the results and text/net
  comparisons fail the reproduction tolerances. The generator used the pilot config, M1 products,
  and M2 sidecar, but rebuilt geometry from `data/teesta_pilot/terrain/domain.gpkg` rather than the
  hand-staged pilot `domain.pol`/XYZ exports.

**Next step:** resolve the Phase 3 input-geometry and field mismatches, generate against equivalent
pilot M1/M2 inputs, then repeat the normalized-file, mesh, and 30-hour output comparisons. Keep the
reproduction marked failed until every required tolerance passes. All pilot inputs are placeholders;
Teesta base flow and other physical data need sourcing before production use.

## 2026-09-27 — M3 output reduction and arrival sampling

- Read the frozen D-Flow FM pilot map/history output with xarray. The 901 map records are 120 s apart; the 1,801 POI history records are 60 s apart. Face maxima are 65.2315 m depth and 29.4403 m/s speed; using the 0.1 m increment over the 7,200 s spin-up state gives first arrivals in 4,033/33,018 faces.
- Exported all face summaries, 90 m EPSG:32645 raster summaries, and full POI histories under `reports/m3_pilot/`. FM map output is snapshot-based; class maps are not per-cell maxima or arrival times, so reductions still need post-processing.
- Changed the production M3 generator map interval from 120 s to 60 s to match history cadence for arrival extraction; the frozen pilot was left unchanged. Added a generator assertion and reran `tests/m3_dflowfm/test_generator.py` (3 passed).
- Regenerated and ran a separate Phase 3 case at `data/teesta_pilot/runs/teesta_pilot_s001/dflowfm_reproduction_map60/case/`. FM completed with 1,801 60-second map samples and passed the `.dia`/map/history success check. The POIs remain dry, depth peaks at 1,841.44 m, and speed peaks at 0.4383 m/s; reproduction remains failed for the documented mesh/input/solution mismatches. Details are appended to `docs/m3_reproduction.md`.

## M3 handoff summary — 2026-09-27

- **Built:** a frozen D-Flow FM pilot with recorded depth, velocity, arrival, and POI histories; a separate active case generator; 60-second map output in generated cases; pilot face/raster/time-series diagnostics; and a successful FM rerun of the generated reproduction case.
- **Still stubbed or placeholder:** M3 contract post-processing is implemented, but the Phase 3 reproduction remains failed: mesh/inputs diverge, generated POIs are dry, and generated maxima are implausible relative to the frozen pilot. Pilot site and physical inputs are placeholders.
- **Next step:** make the generator consume equivalent frozen-pilot geometry and source fields, then implement contract-compliant post-processing and rerun the full reproduction comparison. Keep the result marked failed until all comparisons pass; source placeholder site inputs before production use.

## 2026-09-27 — M3 launcher and M0 queue handoff

- **Built:** `backend/m3_dflowfm/launcher.py` launches a generated FM case detached, captures stdout/stderr in the M0 run log, parses simulation time from `.dia`, and applies only rule 1 to determine success. M0 campaign jobs with a `case_dir` payload now use this path for launch, polling, progress, and recovery; Windows launches go through WSL with converted paths. Added focused rule/progress tests.
- **Still stubbed or placeholder:** M0's other pipeline stages and default runs remain fake; the queue expects a generated `case_dir` payload and does not yet generate an M3 case itself. Contract-compliant M3 post-processing remains unfinished, Phase 3 reproduction remains failed, and pilot/site inputs remain placeholders. This machine's installed kernel reports `MPI : no` and `OpenMP : unavailable`; no MPI partition run or timing comparison is possible with its current tools, so M3 is serial here.
- **Next step:** wire M1/M2 case generation into campaign job creation, then implement contract-format summary outputs and validate the end-to-end generated run against the frozen pilot. Keep production use blocked on sourcing the placeholder inputs. Pytest could not initialize in this environment because its test conftest imports unavailable `geopandas`; syntax and direct launcher checks passed.

## 2026-09-27 — M3 contract post-processing

- Added shared D-Flow FM post-processing in `backend/m3_common/postprocess.py`: max-depth,
  max-velocity, and first `depth > 0.1 m` arrival GeoTIFFs on the canonical grid; long-format POI
  CSV; and schema-validated `run_meta.json`. `backend/m3_pilot/postprocess_dflowfm.py` runs it on
  the frozen pilot. Summary rasters use `domain_mask.tif`, float nodata
  `-9999.0`, and the contract's `extent_m=0.3` / `arrival_m=0.1` metadata.
- The launcher records GNU `time -v` resource usage where available. Generated cases enable FM
  history water-balance output. Raw map deletion is an explicit option and defaults to keep.
- Processed the available pilot output (3,234 mesh faces; 90 m, 623×611 canonical grid). It has no
  positive depth inside `domain_mask.tif`, so metadata warns about mesh/domain alignment. Its
  existing run disabled FM balance output and predates resource capture; those fields are null with
  warnings, not estimates. A balance-enabled repeat was post-processed successfully; it produced a
  water-balance error of -2.65e-8% and captured 104.9 MB peak RAM and solver timing.
- Verification: post-processing, launcher, generator, optional map deletion, and a deliberately
  broken `.mdu` case pass (9 tests). The bad `.mdu` logged `** ERROR` while the kernel returned 0;
  the M3 success check still rejected it.
- **Still stubbed or placeholder:** the pilot site and physical inputs remain placeholders. The
  processed pilot has no positive depth inside its configured domain mask, so mesh/domain alignment
  and the failed Phase 3 reproduction still block treating the result as a usable flood forecast.
  The pilot metadata cannot recover water-balance output or solver peak RAM from its original run;
  newly generated cases now record both.
- **Next step:** fix the Phase 3 geometry/input mismatch so the FM mesh and `domain_mask.tif` cover
  the same modeled area, then rerun post-processing and compare its outputs against the frozen pilot.
  Keep reproduction unaccepted until those comparisons pass and site inputs are sourced.

## 2026-09-27 — M3 pilot moved under backend

- Moved the D-Flow FM builder, case, and attempt records from repository-root `m3_pilot/` to
  `backend/m3_pilot/dflowfm/`, alongside the existing ANUGA archive and shared exports.
- Updated the builder's repository-root and output paths, `.gitignore`, the M3 specification,
  this handoff, and the working-pilot location in `CLAUDE.md`.
- The builder compiles from its new path. A relocated copy of the case ran with no `.dia` errors
  and wrote both map and history outputs, without changing the frozen case outputs.

## 2026-09-27 — M4 3D GPU pilot calibration

- Teesta M1 near-field files are present, but M4 cannot use them yet: the configured inlet is
  `far_field` and the available M3 run has no routed discharge series at that location. Used the
  stock DualSPHysics 3D dam-break example as the permitted fallback, with only `dp` changed.
- Ran GenCase and the GPU solver at 0.0200, 0.0150, and 0.0125 m on the RTX 4060 Laptop GPU.
  Captured GenCase and solver logs, 1-second `nvidia-smi` samples, particle/cell counts, peak
  VRAM, solver and wall runtimes, and run directory size under `backend/m4_pilot/`.
- Updated `vram_estimator.py` to use a count-weighted calibration across all three 3D runs by
  default, preserving the old 2D root logs for comparison. Measured bytes per particle decreased
  8.52% (179.24 B → 163.96 B); bytes per cell increased 119.4%, but cells per particle were
  lower in this stock case. Terrain-cut near-field calibration remains outstanding.
- Updated the M4 pilot decision record with run metrics, calibration formulas, and the limitation
  that the Python near-field generator still has no stock 3D benchmark builder. No pytest suite
  was run for this operational calibration update.

## M3/M4 handoff summary — 2026-09-27

- **Built:** moved the D-Flow FM pilot builder, case, and diagnostics under
  `backend/m3_pilot/dflowfm/`; kept ANUGA and shared exports as pilot records. Added M4 stock 3D
  GPU runs at three particle spacings, preserved GenCase/solver/1-second GPU logs, and switched
  the VRAM estimator's default calibration to the combined 3D measurements.
- **Still stubbed or placeholder:** M3's generated case still fails reproduction against the
  frozen pilot and its Teesta inputs are placeholders. The M4 runs are stock rectangular
  dam-break benchmarks, not a Teesta terrain/inlet case. Teesta's M4 inlet needs an M3 routed
  discharge series that is not available; its terrain-specific particle and cell calibration
  therefore remains unverified.
- **Next step:** resolve the M3 mesh/field mismatch and produce a routed discharge series at the
  Teesta near-field inlet. Then generate and run the real M4 `nearfield.stl` case at multiple
  `dp` values and compare its memory/count behavior with this stock 3D calibration. Keep both
  solvers' results labelled as pilot data until sourced inputs and reproduction checks pass.

## 2026-09-27 — FM/SPH run-budget evidence

- Added `docs/run_budget.md` with two real short serial D-Flow FM timings (33,018 and 98,121 faces), full pilot runtime/post-processing and disk figures, and the three recorded DualSPHysics 3D benchmark timings from `backend/m4_pilot/`.
- Confirmed MPI is unavailable on this host. The refined mesh kernel run succeeded but its written-net reread dropped 134 edges, so it remains a timing-only result and does not pass M3 rule 3.
- Presented 30-day scenario-count/resolution options with a 30% failure allowance and a provisional 90 m recommendation. Site-scale and SPH estimates are marked unverified; Chamoli site config/event inputs and an M4 post-processing benchmark are absent. No choice was added to `docs/decisions.md`.

## 2026-09-27 — M3/M4 build, gaps, and next step

- **Built:** documented two short serial FM timings at 33,018 and 98,121 faces, full pilot post-processing time and raw-map disk costs, plus the three M4 3D benchmark timings and a 30-day scenario-budget set of options in `docs/run_budget.md`.
- **Still placeholder/stubbed:** M3 pilot data are placeholders, and the independent M3 generator has not passed Phase 3 reproduction. The refined timing mesh runs but its net writer reread loses 134 edges. M4 has only stock dam-break benchmarks: no terrain-cut Teesta/Chamoli case or representative M4 post-processing timing. Chamoli site/event inputs are absent; its 2021 event was a mass flow, not a dam breach.
- **Next:** fix the M3 mesh writer/reproduction mismatches, then rerun the reproduction checks before accepting a production mesh. Source and add the Chamoli site/event inputs; then run a terrain-cut M4 case at multiple particle spacings and measure post-processing. Revisit the run-budget choice after those measurements; no option has been recorded in `decisions.md`.

## 2026-09-27 — Teesta MVP timeline and impact artifacts

- Added a direct-run timeline path for registered `delft3d_direct` queries. It reads the retained
  D-Flow FM map NetCDF (901 water-depth snapshots at 120 s cadence through 108,000 s), selects
  five actual records across wet-area growth, rasterizes those records to the same 611×623,
  EPSG:32645, 90 m grid as the solver summary, and renders/caches them through M0's existing PNG
  layer renderer. The existing `median/high/possible` timeline URLs all alias the same real
  deterministic snapshot; UI caveats explain that these are not uncertainty bands. Timeline
  hydrograph values come from the solver's actual input CSV and remain labelled reconstructed.
- Updated the existing playback map adapter in `source.ts`/`app.tsx` to display the selected
  contract timeline raster URL through the existing image-overlay map component. Browser E2E
  confirmed the slider changes the map URL to an actual `/timeline/median_t*.png`, loaded at
  611×623 with no browser errors; no layout redesign or snapshot changes.
- Added M0's direct-run impact derivation, triggered only for registered `delft3d_direct` queries
  without a stored impact file. It intersects the real 0.3 m threshold depth layer with stored
  exposure: 287.8 WorldPop persons, 346 OSM building footprints, 26,596.5 m OSM roads, and 17
  bridges; no affected hospitals/schools or matched warning POIs beyond Chungthang. Monetary loss
  remains null. All impacted counts go in the contract's POSSIBLE column with an explicit caveat
  that one deterministic run is not a probability class. Population carries no artificial range.
- Added source records for OpenStreetMap and WorldPop. Provenance/caveats explicitly state that
  the OSM extraction timestamp and request are missing, the site bbox used for exposure remains
  placeholder-marked, WorldPop is a 2020 ~1 km product disaggregated to 30 m, and cropland /
  hydropower layers are absent. The numbers are limited raster/exposure intersections, not a full
  Teesta impact assessment. FX, price-index, and road-width placeholders still block loss pricing.
- Generated and schema-validated timeline and impact responses for
  `q_20260927T200323Z_042554`; M0 returned timeline (5 frames) and impact (HTTP 200), and each
  real timeline PNG is non-empty. The UI rendered the real timeline frame and displayed the
  exposure values, LOW confidence, missing-input caveats, and unavailable loss. This is an MVP
  dashboard demonstration, not scientific validation.
- Direct snapshot rasters now carry GeoTIFF tags for `REAL_SIMULATION_ARTIFACT`, source run ID,
  source map path, exact model time, and rasterization method. Repeated the complete browser path
  against `q_20260927T204044Z_254686`; all query/impact/timeline schemas validate, M0 query/impact/
  frame endpoints return HTTP 200, and the 611×623 frame image loads in the existing map. The
  direct-run warning table labels its `1` indicator “Inundated” rather than showing it as 100%
  probability; the timeline inspector describes the aliased contract URLs as one deterministic
  frame. Browser screenshots are retained locally at `data/teesta_mvp/dashboard_timeline.png` and
  `data/teesta_mvp/dashboard_impact.png` (ignored runtime artifacts; no visual snapshots changed).
- Validation: `tests/m0_api` + `tests/m6_impact`: **211 passed, 1 hardware-gated skip**; direct
  artifact adapter tests: **2 passed**; frontend typecheck passed, Vite production build passed
  (existing large-chunk advisory), and the full frontend visual suite passed **12/12**.

## 2026-09-27 — M3/M4 run budget reconciled

- **Built:** revised `docs/run_budget.md` to use retained FM metadata for the two short runs
  (26,888 and 33,018 faces), keep the full pilot as the measured post-processing/disk anchor,
  report the three stock SPH calibration timings, and present 30-day resolution/scenario options.
  Removed unsupported short-run claims for a 98,121-face mesh; no matching run artifacts exist.
- **Still stubbed or placeholder:** Teesta pilot inputs are placeholders; the M3 generator's
  reproduction remains failed. M4 has no terrain-cut Teesta or Chamoli run and no measured site
  post-processing/disk delta. Chamoli config/event inputs are absent, so paired FM/SPH capacity
  and a 15-minute end-to-end site demo remain unverified. No campaign or demo option is recorded.
- **Next step:** choose a budget/demo option, then record that choice in `docs/decisions.md`.
  Before committing to a site campaign, source Chamoli inputs, fix the M3 reproduction/mesh issues,
  route discharge to the Teesta near-field inlet, and benchmark the generated M4 case through
  post-processing. Preserve the mass-flow caveat for Chamoli 2021.

## 2026-09-27 — M0 Scene3D endpoint

- **Built:** `/scene3d/{query_id}` now assembles downsampled terrain and median water-surface float32 grids, a shared UTM local frame, vertical exaggeration metadata, and available near-field Delft3D/SPH surfaces. Added restricted asset routes and an aggregate 20 MB cap. Documented the response and binary format in `contracts/scene3d.md`, the schema/example, and handoff §5.9.
- **Still stubbed or placeholder:** M0 still serves the contract example when a query has no persisted `depth_p50.tif` or site terrain. It does not run the emulator or create query artifacts. Delft3D comparison geometry is derived from near-field DEM plus cellwise maximum depth, not a simultaneous timestep; SPH frames are only included when run IDs appear in query provenance.
- **Next step:** connect real query creation/persistence to this route so it reliably writes median-depth assets and provenance run IDs; then validate the assembled scene and cap against an actual paired Delft3D/SPH query. No tests were run in this session.

## 2026-09-27 — M0 status review and frontend session rules

- **Built:** reviewed `backend/m0_api` against handoff contract 0.2.0 and recorded the current
  implementation gaps for this session. Added `frontend/CLAUDE.md` with rules to preserve the
  existing design, follow `STYLE_GUIDE.md`, reuse and pattern-match existing components, avoid UI
  libraries and global styling changes, keep work in the data layer unless directed otherwise, and
  run Playwright visual checks after every change. The instructions point to the screenshot and
  diff commands in `frontend/visual`.
- **Still stubbed or placeholder:** most M0 routes use contract examples or mock files. Flood query
  execution/persistence, impact and validation data, and exports are not wired to real results;
  onboarding stages and solver runs are placeholders. Campaign job stages are undefined, and rerun
  currently queues onboarding rather than reusing terrain. Timeline, compare, and Scene3D use real
  artifacts only when other modules have written them, otherwise falling back to examples. Site
  listing/detail data are also mock-backed, with lookup limited to known sites.
- **Next step:** wire flood query creation and polling to M5 outputs and persist query artifacts
  according to the contract; use those artifacts to connect the remaining result endpoints. Define
  contract-consistent campaign and rerun job behavior before implementing those workflows.

## 2026-09-27 — M0 and frontend gap audit

- **Built:** reviewed `backend/m0_api` and the frontend against the contract and design docs. Added `docs/frontend_gaps.md`, which classifies each requested dashboard capability and identifies the nearest existing component to reuse. The frontend has navigation, views, controls, maps, status treatments and onboarding layouts; its data seam still returns `awaiting` with empty results.
- **Still stubbed or placeholder:** M0's API is mostly backed by contract examples/mocks. SQLite job and recheck schedule persistence exist, but onboarding stages and solver work are placeholders. Frontend integration is not connected: exports cannot download results, job progress is local scaffolding, and several views lack required uncertainty ranges, confidence layers, charts, event comparisons and GEE status controls.
- **Next step:** connect the frontend data seam to M0 and persist real flood query artifacts from M5. Then complete uncertainty-aware summary/map and timeline features against real responses, before wiring impact, comparison, exports and GEE controls.

## 2026-09-27 — M0 contract review and visual snapshots

- **Built:** confirmed all 22 routes in handoff contract §5 have handlers in `backend/m0_api`.
  M0 also has JSON Schema validation, a SQLite registry/job system, a separate worker, recheck
  scheduling, and helpers for map rendering, timelines, comparisons, Scene3D, and static files.
  Added a Playwright visual suite under `frontend/visual` and `npm run test:visual`, with one
  1440×900 baseline screenshot for each of the app's 12 routes. Captures request reduced motion
  and disable animations. No component code changed; all 12 snapshot checks passed.
- **Still stubbed or placeholder:** route presence does not mean the full workflow is connected.
  Flood query submission/polling, site data, impact, validation and exports remain mock-backed in
  part or whole. Worker stages and solver runs are partly simulated. Rerun queues onboarding work
  instead of reusing terrain. Timeline, compare and Scene3D use persisted artifacts when available
  and otherwise fall back to examples. The contract remains a draft.
- **Next step:** wire flood query creation and polling to M5 outputs and persist contract-shaped
  query artifacts; then connect dependent result routes to those artifacts. Define contract-
  consistent campaign and rerun stages before implementing those workflows.

## 2026-09-27 — M3 D-Flow FM campaigns

- **Built:** added M5 scenario-design generation/selection to `backend/campaign.py`, named extra
  selection, dry-run and truncated demo options, M0 registry/worker execution, one retry, resumable
  detached runs, contract post-processing, an M5 run-cache index, and atomic campaign progress.
  Demo uses one 9,000 s run, 90 m mesh spacing, 120 s map output, capped at 33,018 faces.
- **Verified:** synthetic fake-solver campaign tests retry then post-process/cache successfully;
  targeted M0/M3/campaign tests pass. A real D-Flow FM demo completed end-to-end on synthetic M1
  terrain (14 faces, 9,000 s simulated, 0.14 s solver runtime; placeholder caveats retained).
- **Blocked for production campaigns:** Teesta's M5 design can still be blocked by placeholder
  breach-width inputs in `sites/teesta.yaml`. `sites/rishiganga.yaml` and its terrain are absent, so a Chamoli campaign cannot
  yet be configured without sourced inputs.
- **Still stubbed or placeholder:** M5 training, validation, and emulator publication do not run
  after a campaign; the new M5 cache is a run index, not a trained library. SPH campaigns still
  only generate/queue cases. Synthetic-demo outputs retain placeholder caveats.
- **Next step:** source the missing M2/site inputs and Chamoli configuration, then run each real
  campaign. Wire successful campaign caches into M5 training and publication afterward.

## 2026-09-27 — XZ9 breach-width range and scenario-design path

- **Built:** repaired the existing XZ9 B_ave implementation with the fixed model constant
  `XZ9_REFERENCE_HEIGHT_M = 15.0` m. M2 now computes XZ9 and Updated DFM breach widths and combines
  them using the existing `min/max` method-range policy. No site-level `h_r` field or parallel
  implementation was added. M5 scenario-design generation consumes this M2 width range.
- **Verified:** the focused M2, schema, and scenario-design suite passed (173 tests). The synthetic
  integration path produced XZ9 B_ave = 53.913732 m, Updated DFM B_ave = 63.205150 m, and a valid
  width range [53.913732, 63.205150] m; scenario design generated 8 scenarios without an `h_r` site
  input.
- **Still stubbed or placeholder:** XZ9 peak discharge remains unavailable, so dependent Q_p fusion
  ranges remain blocked. Teesta still has placeholder breach inputs, including water volume needed
  to build its scenario design. M5 training, validation, and emulator publication remain unwired
  after campaign runs; SPH campaigns still only generate and queue cases.
- **Next step:** source the missing Teesta M2 physical inputs, then generate and validate its real
  scenario design before launching a campaign. Separately, resolve and implement the XZ9 peak-
  discharge path if Q_p ranges are required, and wire campaign outputs into M5 training/publication.

## 2026-09-27 — Synthetic engineered-dam onboarding demo

- **Built:** added `sites/synth_engdam.yaml` from the existing synthetic site, M1 valley raster
  generator, and test-only sourced inputs; connected contract JSON onboarding jobs to M1 terrain,
  M2 breach ranges, M5 four-point scenario design, M3 case/campaign execution, post-processing,
  and M5 emulator training plus LOOCV. Demo campaigns use four distinct design points at 90 m and
  a common horizon long enough for the longest M2 hydrograph (9,150 s on this fixture). M3 stop
  times now align with its 30 s timestep, and the worker waits for D-Flow's process to exit before
  opening output files.
- **Verified:** one API-created demo reached `ready` in about 10 s on this machine. All four
  D-Flow FM cases ran and post-processed; M5 trained and completed four LOOCV folds. The manifest
  carries `DEMO MODE` and `LOW`. M4 generated a valid near-field case spec for each design point;
  no DualSPHysics solver run was part of onboarding. M0 focused tests: 156 passed, one unrelated
  static GeoJSON fixture test failed because its expected file is absent. M1: 61 passed; M2: 130;
  M3: 5; M4 generator: 7; M5: 224 passed.
- **Still stubbed or limited:** this repository does not contain the referenced
  `engineered_dam` FailureSource fixture; the new demo uses existing synthetic test values and is
  clearly labelled synthetic. M1 still requires raw DEM/land-cover inputs and exactly one selected
  DEM product; automatic download/comparison/selection is not wired into onboarding. The shared
  Manning table is placeholder data, so M1/M3 output retains those caveats. M4 case generation is
  validated, but SPH execution/training is not included in onboarding.
- **Next step:** add the missing FailureSource fixture if it is expected to be authoritative, then
  wire real-site DEM selection and decide whether onboarding should launch an SPH comparison after
  the Delft3D library is ready.

## 2026-09-27 — Integration audit handoff

- **Built:** canonical JSON onboarding now runs M1 → M2 → four-point M5 design → M3 Delft3D
  campaign/post-processing → M5 training and LOOCV for the synthetic engineered-dam demo. M4
  case generation accepts the same scenario inputs. The demo reached `ready` with a `DEMO MODE`,
  `LOW` confidence manifest; no onboarding contract fields were added.
- **Still stubbed or placeholder:** raw DEM/land-cover acquisition and DEM selection remain an
  operator-prepared input to M1; the shared Manning table remains placeholder data; the referenced
  `engineered_dam` FailureSource fixture is absent; M4 is case-generation/handoff only, with no
  DualSPHysics execution or SPH training. The M0 suite has one unrelated missing-Teesta-GeoJSON
  fixture failure (156 passed, 1 failed); focused synthetic onboarding tests pass.
- **Next step:** supply the missing FailureSource fixture if it is authoritative, then implement
  real-site raster acquisition/selection and validate the same onboarding path against a real site.
  Treat SPH execution and training as a separate follow-on after its solver environment is ready.

## 2026-09-27 — Frontend contract API client

- **Built:** added a centralized contract-route client with `VITE_USE_MOCKS` and
  `VITE_API_BASE_URL`; JSON mocks import the canonical `contracts/examples/` payloads and
  `contracts/styles.json`. Updated `frontend/API_USAGE.md` to document route coverage and the
  remaining legacy canvas adapter limits.
- **Verified:** frontend TypeScript check and production build pass; `npm run test:visual`
  passes all 12 screenshots with no visual changes.
- **Still limited:** the existing canvas `Grid`/`Result` models cannot represent the contract's
  estimate envelopes and file-backed layers. Scenario listing and saved runs still have no
  contract routes. Impact totals are still computed from legacy local arrays rather than contract
  `Estimate` objects. Contract API data is available through `src/data/api.ts`; affected views retain
  their existing placeholders until migrated to consume those response shapes.
- **Next step:** migrate the map and summary views to contract `FloodQueryResponse`, `Scene3D`,
  `Timeline`, and `Impact` shapes, including layer file loading and estimate presentation. Keep the
  no-route scenario and saved-run views as explicit empty states unless their contract is extended.

## 2026-09-27 — Frontend site selector and status

- **Built:** added a sidebar active-site selector populated from contract `GET /sites` through
  `src/data/api.ts` and the new `source.listSites()` adapter. The selected site's `site_id` is
  passed to rapid and physics flood-query requests. Site status badges cover ready, onboarding,
  demo mode, outdated and failed states. New selector copy is in `frontend/src/content/ui_text.json`.
- **Verified:** `npx tsc --noEmit`, `npm run build`, and `npm run test:visual` passed; all 12
  visual snapshots pass without updates. The screenshot uses the existing API contract fixtures.
- **Still stubbed or placeholder:** site summaries do not provide terrain grids, so selecting a
  site does not populate the legacy map `Grid`; terrain, result layers and impact views still use
  awaiting/empty adapters or local legacy data. The selector changes the active site context, but
  the remaining screens are not yet migrated to contract-shaped result data.
- **Next step:** migrate the map and summary flow to the active site's contract flood-query,
  scene/timeline layer files and `Estimate` uncertainty shapes, preserving explicit awaiting and
  placeholder states where the API has no usable result.

## 2026-09-27 — I-1 deterministic synthetic E2E plumbing

- Added the explicitly synthetic `demo_valley` site fixture (`sites/demo_valley.yaml` and
  `tests/fixtures/i1/site_config.json`) and a bounded deterministic artifact producer in
  `backend/m0_api/synthetic_demo.py`. The 48×48 EPSG:32645 fixture writes canonical terrain,
  domain, depth, velocity, arrival and extent artifacts plus schema-shaped run metadata.
- `POST /sites` with `site_id: demo_valley` and `demo_mode: true` persists the accepted config and
  queues the existing SQLite onboarding job. The existing worker advances the normal lifecycle,
  creates the synthetic files, skips all physical solvers, and reaches `ready`. Other demo sites
  retain their prior M1/M2/M5/D-Flow FM onboarding path.
- `POST /flood/query` reads the generated run rasters, writes query-layer GeoTIFFs, extent GeoJSON,
  timeline inputs, result and impact artifacts, and registers the query. Polling, styled raster PNG,
  extent, impact, timeline frames, compare-unavailable status, validation-unavailable status, and
  GeoJSON/KML/shapefile/PDF exports now use or identify the same demo artifacts. Missing demo query
  artifacts return 404; result values carry LOW confidence, `demo_mode`, synthetic caveat, and
  inherited placeholder fields.
- Added `getFloodRasterOverlays()` in the frontend source seam: it passes contract image URLs,
  bounds, style IDs and units through without inventing a legacy Grid array. The current UI map still
  consumes the legacy Grid/Result shape and does not display this contract overlay; panel wiring
  was intentionally left unchanged per I-1 scope.
- Focused I-1 integration test passed and compares the API maximum depth to the generated depth
  GeoTIFF, validates run/result/impact/timeline schemas, checks a real shapefile bundle, and covers
  invalid site/query and missing layer errors. The backend suite excluding `tests/m3_dflowfm` passed:
  886 passed, 6 skipped. The stale `test_get_file_geojson` assertion now checks the intended 404
  rather than expecting an unrelated example fallback. Full pytest collection remains blocked by
  pytest's rejection of `pytest_plugins` in `tests/m3_dflowfm/conftest.py`.
- Frontend TypeScript check, production build and all 12 visual snapshots passed; no snapshots
  changed. This verifies software artifact handling only, not Delft3D, DualSPHysics, breach
  equations or scientific forecast accuracy.

## 2026-09-27 — I-1 frontend contract raster bridge

- `source.floodRasterOverlay()` now adapts the `FloodQueryResponse` layer reference already
  returned by the query flow into a typed image overlay (API-resolved URL, bounds, style ID, unit).
  It does not create a legacy `Grid` or refetch the query. Mock mode alone may select the example's
  `p_inundation` layer when `depth_p50` is absent; real mode reports the missing layer as empty.
- `TerrainMap` keeps its existing canvas path for legacy `Grid`/`Result` data and renders a
  contract raster PNG when supplied. The backend style colors/breaks drive the existing legend;
  the layer bounds are retained with the overlay, which occupies the map extent. The app selects
  the 2D tab for a raster response. Synthetic area/depth/velocity estimates remain visible when
  peak discharge is null.
- Browser verification against the live M0 API loaded the generated `depth_p50.png` (48×48,
  EPSG:32645 source GeoTIFF); its 966 opaque pixels match the source raster's 966 wet cells, and
  the rendered summary shows 9.7 km², 5.2 m, and 2.9 m/s. The response bounds/style ID are present
  on the map element. DEMO MODE, PLACEHOLDER and LOW confidence are visible.
- Mock mode renders its contract-example `p_inundation` URL and receives the existing 1×1
  transparent mock PNG. The interactive synthetic screen was visually inspected; the 12 existing
  screenshot snapshots pass unchanged. Frontend typecheck/build pass; focused M0 I-1/endpoints/
  schema tests: 94 passed. This closes the I-1 display gap only; no real-solver or science claim.

## 2026-09-27 — I-2 implementation blockers: controlled reproduction and artifact seams

- Audited the retained D-Flow FM pilot against prior generated cases. The 3,234-face generated
  result was pre-boundary-segmentization; the 77,415-face version still used the full M1 domain,
  connected components and POIs, and a derived outlet, while the retained 33,018-face pilot uses
  clipped L002/L004/L006 reach geometry and its explicit outlet. Historical `sourcesink_discharge`
  errors came from rejected boundary experiments; current pilot forcing is a point source.
- Added a pilot-only reference mode to the existing M3 generator. Its mesh (18,034/51,051/33,018),
  bed and Manning samples, outlet, POIs and source time series match the frozen pilot; generated
  inputs pass net round-trip checks. A controlled 30-hour D-Flow FM rerun completed with no `.dia`
  errors, nonempty 481,977,476-byte map and readable history, and all common solver map/history
  variables equal the retained pilot exactly. Runtime 468.5 s; peak RAM 228.1 MiB. This closes
  reproduction for the frozen pilot setup only; the production M1 domain and Teesta input gate
  remain unresolved. Details: `docs/m3_reproduction.md`.
- Added the controlled M4 launcher to campaign/worker execution and reran the stock 3D DualSPHysics
  benchmark through both direct launcher and registered-worker paths. Solver/GenCase return codes
  were 0; about 11 s; sampled peak VRAM 845 MiB. Logs and sampling evidence are retained under
  `backend/m4_pilot/controlled_launcher_20260927/`. This is not a terrain or target-site run.
- Added contract 0.3.0 `routed_discharge.json` + `timeseries.csv` (§4.4) and M4 input consumption.
  Controlled M3-series fixture round-trips and M4 converts its values to inlet velocity; it does
  not establish a production flow-extraction method. Site-specific section geometry, integration
  method and time alignment remain blocked. Cascade trigger scope remains `CONTRACT DECISION
  REQUIRED` with dependencies/fields recorded in `docs/decisions.md`.
- Added `backend/m0_api/real_query.py`: a direct query now requires an explicit registered scenario
  and completed/postprocessed run, validates its run metadata and georeferenced summary rasters,
  copies actual depth/velocity/arrival rasters into query paths, derives extent from actual depth,
  and returns direct-solver estimates with LOW confidence and source run provenance. Missing real
  runs/artifacts return errors rather than contract examples. Controlled M0 test exercises
  registration → response → polling → rendered raster/extent and schema validation.
- Added `docs/real_input_checklist.json` for Teesta and Rishi Ganga. Both production gates are
  BLOCKED; no scientific/site configuration values were changed. Chamoli's 2021 mass-flow event
  remains incompatible with the current dam-breach setup pending an event representation decision.
- Focused checks: M3 reproduction/artifact tests passed; final M0/M3/M4/contracts regression run:
  **116 passed, 1 skipped**. Full repository suite: **903 passed, 8 skipped**. The one M4 skip is
  the optional controlled binary test when `DSPH_BIN_DIR` is unset; both controlled DualSPHysics
  launcher/worker tests were separately run with the installed binaries and passed. `git diff
  --check` and machine-readable input-checklist JSON parsing pass. No target-site production solver
  run or campaign was started.

## 2026-09-27 — Teesta MVP forcing prepared; solver launch awaits run-scope resolution

- Verified the historical Teesta III figures against PARIVESH's explicit "Existing Salient
  Features" column, separately from the proposed replacement concrete-gravity dam. Added
  `src_043`–`src_046` entries in `docs/data_sources.md`. The cited White Rose reconstruction
  reports a modelled ~5,340 m³/s Chungthang peak and arrival near 00:30 IST; a separate 2025
  reconstruction reports a modelled ~7,355 m³/s Chungthang peak. Neither is a complete observed
  hydrograph. Kept the user's 7,355 m³/s / 03:20 constraints explicitly as MVP reconstruction
  targets, not observations.
- Added `backend/m3_dflowfm/mvp_forcing.py` and tests. The generated ignored input
  `data/teesta_mvp/inputs/teesta_2023_mvp_forcing.csv` is a 60-second sampled triangle above
  500 m³/s, with 50,000,000 m³ integrated excess volume and a 03:20 IST peak. Duration is
  14,587.892 s (T = 2V/(Qpeak-Qbase)); sidecar says `MVP_RECONSTRUCTED` and
  `NOT_OBSERVED_HYDROGRAPH`. The sampled trapezoidal volume matches the target exactly.
- Exercised M2 methods using only the supplied Teesta III reservoir/dam facts plus overtopping
  mode. F95/F8 yield method-specific width estimates 76.89/57.85 m; failure-time methods F95/F8
  yield 822.07/757.99 s. The contract's recommended width pair remains blocked because XZ9 needs
  unsourced rockfill erodibility; M2 also requires a breach-height input. No width/depth was
  selected or added to site configuration.
- **No solver launched:** the frozen pilot's single source is at South Lhonak, whereas the
  7,355 m³/s and 03:20 constraints are at Chungthang; applying them at that source cannot be
  interpreted as a Chungthang hydrograph match. The pilot M3 case has one source, so adding the
  Teesta III reservoir breach would be a second source/cascade behavior not present in that case.
  Awaiting MVP run-scope clarification; no solver or frontend result is being represented as real.
- Tests: `tests/m2_breach`, `tests/m3_dflowfm` (including MVP forcing), and M0 schema tests:
  **170 passed**. No M0 query, dashboard, export, frontend, or production solver run was attempted.

## 2026-09-27 — Teesta MVP real D-Flow FM run reached the dashboard

- Prepared `backend/m3_dflowfm/mvp_case.py` to copy the retained frozen pilot case and replace
  only the South Lhonak source `.tim` with the existing deterministic MVP forcing. Geometry,
  source point, bed/Manning samples, boundaries, POIs, 30-hour stop and pilot solver configuration
  are retained. Metadata records `MVP_PILOT_DOMAIN`, `MVP_RECONSTRUCTED`,
  `NOT_OBSERVED_HYDROGRAPH`, single source, and explicitly no Teesta III breach/cascade.
- First solver attempt exited 0 but was rejected by rule 1: `.dia` reported end-of-file on the
  source series at 14,610 s, before TStop. Preserved its `.dia`, partial map/history and resource
  record. Fixed only the input coverage defect: the D-Flow `.tim` now holds the existing 500 m³/s
  baseline through 108,000 s. One retry completed with return code 0, no `** ERROR`, 108,000 s
  simulated, 750.08 s runtime, 750 s wall time, 223.90 MB peak RSS and 574,455,934 bytes
  measured run-directory size. The NetCDF map has 901 records/33,018 faces; history has 1,801
  records/4 stations; both are readable and reach model end.
- Existing M3 post-processing wrote canonical 611×623 90 m rasters on EPSG:32645, the extent
  vector, POI time series, and schema-valid run metadata. Depth has 2,423 wet cells and 66.8905 m
  maximum; velocity maximum is 32.7657 m/s; M0-derived inundated area is 19,043,100 m². The
  query's copied depth TIFF is cell-for-cell identical to the registered run's source depth TIFF.
  The raw FM NetCDF emits EPSG:0 metadata, but canonical rasters use the frozen pilot's validated
  EPSG:32645 grid transform. The frozen configuration did not emit mass-balance output.
- Registered `teesta_2023_mvp__delft3d` in M0 and created real direct query artifacts. The current
  query ID and complete machine-readable evidence are in
  `data/teesta_mvp/mvp_execution_summary.json` (gitignored with run artifacts). M0 polling, map
  PNGs, extent GeoJSON and all four export formats returned success. Frontend real mode fetched
  the generated PNG through `api.ts → source.ts → TerrainMap`; browser measured 611×623 pixels,
  displayed 19.0 km² / 66.9 m / 32.8 m/s, LOW confidence and the explicit reconstructed-forcing
  warning. Screenshot: `data/teesta_mvp/dashboard.png`.
- Impact and timeline are explicitly unavailable (404 `artifact_not_found`): no impact result or
  timeline frames were produced. No examples were substituted. Compare has no paired solver or
  validation artifact. There is no M5 training result and no scientific validation claim.
- Verification: selected M2/M3/M0/schema tests **244 passed**; frontend `tsc --noEmit` and
  production build passed; visual suite **12 passed** after updating only the dashboard and
  simulation snapshots for the intentional real-mode change from “demo people” to “people”. No
  campaign, M4 run, second source, or additional target-site solver run was started.

## 2026-09-27 — Real Teesta timeline and impact dashboard artifacts

- Added `backend/m0_api/real_timeline.py` to select five snapshots directly from the registered
  D-Flow FM map NetCDF (source cadence 120 s, 901 records through 108,000 s), rasterize actual
  water depth onto the canonical 90 m EPSG:32645 grid, and record the source run, map file,
  snapshot times, POI history and reconstructed-forcing provenance. The real Teesta query
  `q_20260927T204044Z_254686` now serves five playback frames through the existing timeline
  endpoint. Timeline URL bands alias the same deterministic snapshot because this single-run MVP
  has no probabilistic percentiles; UI copy states this explicitly.
- Added `backend/m0_api/real_impact.py` to derive the contract impact response from the query's
  real solver depth/extent and stored exposure layers. It reports 287.8 WorldPop persons,
  346 OSM buildings, 26,596.5 m of OSM roads and 17 bridges intersecting the direct footprint;
  the warning table uses registered FM histories. INR loss stays null because required conversion
  and road-width inputs remain placeholders. Impact labels one-run extent as a deterministic
  possible footprint, not a probability. Coverage records the placeholder bbox, unknown OSM
  fetch date, coarse 2020 population raster and absent cropland/hydropower layers.
- Connected the existing map/playback component to timeline frame overlays through a typed
  `source.ts` adapter. Direct-run labels say “In this run” rather than probability, and the
  playback inspector shows the single D-Flow snapshot honestly. No map redesign or new data-fetch
  path was added.
- Checked the dashboard in real mode against the already registered run: map/timeline showed
  the generated FM snapshot, impact showed the run-derived exposure counts, and the UI retained
  LOW confidence, MVP reconstructed forcing and pilot-domain caveats. Browser console had no
  errors. Screenshots: `data/teesta_mvp/dashboard_timeline.png` and
  `data/teesta_mvp/dashboard_impact.png` (ignored runtime artifacts).
- Validation: M0/M6 impact and direct-adapter tests **211 passed, 1 skipped**; latest direct
  adapter rerun **2 passed**; frontend typecheck, production build and visual suite **12 passed**.
  API returned schema-valid impact and timeline responses (five frames); the final timeline PNG
  was nonempty and 611×623. No snapshots were updated in this task. No solver was rerun and no
  scientific or historical-event validation is claimed.

## 2026-09-27 — Teesta MVP dashboard stabilization

- Connected the home/landing Teesta entry to the registered direct D-Flow FM query. The landing
  page now names the Teesta MVP run and leaves extent blank until a result has loaded instead of
  showing the legacy screening model's zero result. An expected `getTerrain()` awaiting response
  is no longer surfaced as an API error banner; terrain remains explicitly unavailable in the
  legacy Grid view while the contract raster renders in the flood map.
- Monitoring no longer shows a nonfunctional observed-vs-simulated swipe when no georeferenced
  observed extent is registered. It reports that limitation and displays the two actual cached
  Sentinel-2 images with acquisition dates (2023-09-26 and 2023-10-26).
- Captured `data/teesta_mvp/demo_ready_dashboard.png`: the real M0 depth PNG loads at 611×623 and
  shows the real D-Flow FM run's 19.0 km² flooded area, 66.9 m maximum depth and 32.8 m/s maximum
  cell velocity. Timeline playback loads a solver-derived 611×623 frame PNG. Impact reports the
  registered WorldPop/OSM-derived output; loss remains null/placeholder and confidence LOW.
- In one browser session, the Teesta Demo entry opened the registered result and all major routes
  rendered without console errors or failed requests. Cached satellite images loaded at 902×1010.
  SHP/KML/PDF exports from the generated query downloaded at 132,194 / 358 / 827 bytes.
- Compare correctly remains UNAVAILABLE: the only actual SPH execution is the controlled stock
  tank benchmark, which is not spatially or scenario-wise comparable to the Teesta river run.
  No Teesta paired SPH artifacts or defensible comparison metrics exist; no metrics were fabricated.
  Validation correctly reports no validation runs. This comparison gap means the requested MVP
  with a meaningful Delft3D-vs-DualSPHysics result is **not complete**.
- Reviewed and updated the 12 existing route snapshots for intentional demo-state/data changes and
  added the new Validation snapshot. Full visual suite: 13 passed. Frontend typecheck and production
  build pass. `.venv/bin/pytest -q tests/m0_api tests/m5_emulator/test_compare.py`: 175 passed,
  1 skipped. With `DSPH_BIN_DIR` set, M4 launcher/worker suite: 77 passed. No Teesta solver was run.

## 2026-09-27 — Teesta MVP SPH comparison attempt (blocked)

- Added `backend/m4_sph/teesta_mvp.py`: it derives an MVP-only 300 m section at the retained
  Chungthang POI from the actual M3 UGRID `waterdepth` and `ucmag` variables, integrates
  depth × speed magnitude × intersected face length, and writes the existing routed-discharge
  manifest/CSV with provenance. The lack of an M3 velocity vector is explicit; magnitude is treated
  as section-normal velocity only for this MVP approximation. The 600 s window is taken from M3
  history around the real Chungthang peak. The M4 inlet width is based on the actual wet-face span.
- Cropped the existing EPSG:32645 M1 Teesta DEM to 600×600 m at 10 m resolution around the retained
  Chungthang POI, and recorded bounds, CRS, source path and transform method. An STL opening was
  cut only across the M3-wet inlet segment, keeping the input terrain values unchanged. Case
  generation passed GenCase (`returncode=0`, about 1.8 s); predicted VRAM was 393 MiB.
- Registered `teesta_2023_mvp__dualsphysics` via the existing M0 campaign worker. Both attempts
  stopped before simulation progress at `JSphInOut::InitCheckProximity`: actual solver-generated
  `CfgInOut_ErrorParticles.vtk` points have x/y correlation −0.997, showing the inlet width plane
  aligned along the channel rather than across it. GenCase returned 0; DualSPHysics returned 1.
  Attempt 0: 3.686 s total, 867 MiB sampled GPU memory. Retry attempt 1: 3.026 s, 877 MiB. Both
  logs and failed raw artifacts are retained under
  `data/teesta/runs/teesta_2023_mvp__dualsphysics/attempts/`.
- Fixed the demonstrated DualSPHysics clockwise `rotateaxis` sign in `generator.py`, with a unit
  regression check. Also fixed M0 campaign failure handling so a failed M4 campaign attempt uses
  its configured single retry and a failed/queued case cannot be labelled as a real solver output.
  The corrected rotation was not run: the one retry was consumed before the VTK orientation was
  fully diagnosed. No third solver attempt was made.
- Added a common-footprint comparison builder and a strict M0 file route. It requires successful,
  postprocessed artifacts from both real runs; no comparison sidecar or difference raster was
  produced because the SPH run failed. `GET /compare/teesta?scenario_id=teesta_2023_mvp` returns
  `available:false` and only the D-Flow run ID, not the Compare example. Therefore the dashboard
  still has no paired Teesta SPH result and the Model Comparison acceptance criterion remains
  blocked.
- Validation after the implementation changes: relevant M4 and M0 tests **142 passed, 8 skipped**;
  frontend typecheck and build passed; visual suite **13 passed** with no snapshot updates. These
  controlled tests validate the software seams only, not a successful Teesta SPH execution.
- **Still blocked/stubbed:** no successful Teesta DualSPHysics integration, postprocessed SPH depth
  raster, common-footprint difference raster, paired-run metrics, or available Model Comparison
  response. The current run remains classified as a failed solver attempt; the route correctly
  returns `available: false`. The corrected inlet rotation is covered by a unit test but has not
  been verified in a solver run. The M3-derived inlet also remains an MVP approximation because
  the retained map provides speed magnitude, not section-normal velocity.
- **Next step:** after deciding whether to authorize another controlled Teesta SPH attempt, verify
  the corrected inlet orientation with the solver, then proceed only if the run completes and its
  existing postprocessor emits georeferenced artifacts. Otherwise retain the unavailable comparison
  state. No real-site campaign, M4 production coupling, or scientific validation is implied here.

## 2026-09-27 — Teesta MVP comparison: corrected SPH run completed

- **Built:** the corrected `teesta_2023_mvp__dualsphysics` case was prepared and launched through
  the existing M0 worker as attempt `a02`. GenCase and DualSPHysics 5.4.355 both returned 0; the
  solver completed the 600 s window in 705 s, with 993 MiB peak VRAM and 375.9 MiB sampled peak
  process RSS. Its inlet direction matches the local channel tangent and its inlet width axis is
  perpendicular. The run produced 121 PART frames, georeferenced 10 m summary rasters, and 21
  surface GLBs. Logs and resource samples are retained under
  `data/teesta/runs/teesta_2023_mvp__dualsphysics/attempts/a02/`.
- **Comparison/dashboard:** the real D-Flow and SPH runs are paired in
  `data/teesta/compare/teesta_2023_mvp/compare.json`. The common EPSG:32645 footprint contains
  3,600 valid 10 m cells; the depth-difference GeoTIFF and PNG route are live. The existing Compare
  endpoint defaults to this Teesta MVP pair, so the page displays both run IDs, the difference
  layer, computed metrics, and caveats. Only the Compare screenshot was updated for the genuine
  paired result.
- **Still limited or stubbed:** the Teesta site configuration still has 50 placeholder fields and
  the M3-derived inlet discharge remains an MVP approximation based on speed magnitude because no
  velocity vector is available. The solver emitted no gauge CSVs, so no probe timeseries or arrival
  comparison is registered. SPH mass-balance error is not computed. The solver also warned that
  more than 100% of current fluid particles were excluded in one output; the derived depth reaches
  259.5 m despite an 11.8 m inlet height. The resulting metrics are actual calculations from the
  produced rasters, but are not validated model performance. Rishi Ganga, full-domain coupling, and
  scientific/observational validation remain out of scope or unstarted.
- **Validation:** relevant M4, M0 and Compare tests: **254 passed**; run/Compare schema and
  georeferenced artifact checks passed; frontend typecheck and production build passed; visual suite
  **13 passed**.
- **Next step:** investigate the particle-exclusion warning and anomalous SPH depth field, and fix
  the gauge-output/postprocessing gap before interpreting or presenting the comparison metrics as
  meaningful. Preserve the current run and comparison as a caveated demo artifact; do not launch
  another solver attempt until those issues are understood.

## 2026-09-27 — Demo stabilization pass, item 0: checkpoint commit

- All uncommitted Teesta MVP work (real D-Flow FM run wiring, timeline/impact adapters, the SPH
  comparison attempt, frontend real-mode dashboard, visual snapshots — roughly 100 files) was
  reviewed for secrets/large binaries and committed in two commits: backend/contracts/docs/tests,
  then frontend. The repo was found already on `main` (not `m0-job-system`) at session start, from
  a branch switch that happened before this session; the user chose to keep the work on `main`
  rather than move it to a feature branch.
- No solver run, campaign, or scientific claim was added in this item.

## 2026-09-27 — Demo stabilization pass, item 1a: honest D-Flow headline caveats

- **Root cause, confirmed against the actual registered run
  (`teesta_2023_mvp__delft3d`/`case_retry1`):** the 66.9 m max-depth cell (t=15,480 s, still 63.3 m
  at run end) sits in one of 136 closed depressions in the unconditioned M1 far-field DEM (the two
  deepest pits are 60.8 m and 91.7 m of priority-flood fill); about 11% of the wet area at the
  90 m grid sits in such pits. The 32.8 m/s max-velocity cell sits on a steep gorge reach (DEM
  slope 0.12–0.74 m/m near the extreme cells); about 57% of the wet area is on reaches this steep.
  Neither is a solver artifact — both are real numerical output of a physically-implausible input
  (an unconditioned DEM) and an unmodelled process (clear-water flow standing in for a
  debris-laden event).
- Added `backend/m0_api/dem_diagnostics.py`: reuses the project's own priority-flood
  implementation (`backend.m1_terrain.hydro.route`, chosen over adding a richdem dependency to the
  query path) to classify each summary-grid cell as sitting in an unconditioned DEM depression
  and/or on a steep reach, against thresholds in the new `config/m0_direct_query.yaml` (not a
  `SourcedValue`; this is caveat-detection tuning, not a physical coefficient). Depression/slope
  rasters are computed once per DEM and cached (`ensure_cached_diagnostics`); on the frozen pilot's
  623×611 90 m DEM this takes under a second.
- `backend/m0_api/real_query.py` now runs this classifier (delft3d runs only) and adds
  `dem_depression_ponding` / `clear_water_steep_reach_velocity` caveats to the direct-run response
  when the actual max-depth/max-velocity cell is flagged, plus a machine-readable
  `provenance.diagnostics` block (pit/steep-reach fraction of wet area, whether each extreme cell
  is flagged) — no contract version bump; `Provenance` already allows additional properties.
  `max_depth_m`/`max_velocity_ms` are unchanged (still the real single-cell maxima; never tuned).
- `backend/m0_api/real_impact.py` now flags POIs that never wet despite a wet cell within
  `poi_snap_search_radius_m` (300 m) as a grid-snapping artifact rather than silently dropping them
  as "not affected". On the real run this correctly separates Sangkalang bridge (127 m from a wet
  cell) and Mangan hospital (90 m) — genuine snapping — from Lachen (597 m away — genuinely dry),
  adding a `poi_grid_snapping` caveat and a named `data_coverage_notes` entry only for the former
  two.
- Frontend: added a `DirectRunMetrics` component (`app.tsx`) so the direct-run headline shows
  flood extent plus the first affected POI's peak depth/velocity (Chungthang: 11.6 m, 5.5 m/s),
  with the single-cell domain maxima demoted to a labelled "artifact, see caveats" line. Added the
  four new caveat strings to `ui_text.json`. PDF export (`main.py`) appends the same caveat
  explanations next to the printed maxima.
- Verified against the real registered run end-to-end: direct Python calls, a temporary local
  server (port 8010) hit over real HTTP for both `/flood/query` and `/impact/{query_id}`, and a
  throwaway Playwright script confirmed the dashboard renders the new headline with no console
  errors. (Separately noticed: the user's own long-running dev backend on port 8000, started at
  22:48 before this item's edits, is serving stale code — restart it before relying on it again.)
- Tests: `tests/m0_api/test_dem_diagnostics.py` (7, pure-function unit tests on synthetic DEMs) and
  `tests/m0_api/test_real_query_diagnostics.py` (2, end-to-end through `resolve_registered_run`
  with a synthetic pit+step DEM, and confirming the SPH path skips this classifier). Full
  `tests/m0_api tests/m1_terrain` run under the `sih26` conda env: **239 passed, 1 skipped**.
  `tests/m4_sph tests/m3_dflowfm tests/m3_common` added: **264 passed, 8 skipped, 1 failed** — the
  one failure (`test_pilot_reference_builder_reuses_frozen_geometry_fields_and_forcing`) is
  confirmed pre-existing (reproduces identically on the commit before this item) and unrelated to
  this change. Frontend `tsc --noEmit` and `npm run build` pass. The visual suite (13 passed) is
  unaffected because it never triggers a real flood query, so it never exercises the changed
  headline path — a gap item 5's walkthrough is meant to close.
- **Declined by user, not done:** re-running D-Flow FM with a conditioned bed (item 1b). Recorded
  here as the actual fix: conditioning the M1 far-field DEM (breach/fill its pits) before mesh
  generation would remove the ponding artifact; the steep-reach velocities would likely remain,
  since they are slope-driven rather than a DEM defect.

## 2026-09-27 — Demo stabilization pass, item 2: SPH comparison gated; MeasureTool parser fixed

- **Item 2 (required):** `backend/m4_sph/compare_mvp.py` now refuses to publish paired SPH/D-Flow
  metrics when the SPH run's `run_meta.caveats` carries `sph_particle_exclusion_warning`
  (`_unavailable_comparison`, checked before any grid/mesh work). Regenerated the real
  `data/teesta/compare/teesta_2023_mvp/compare.json` through this builder against the actual
  registered runs: `sph_vs_delft3d.available` is now `false`, `run_ids` is `[teesta_2023_mvp__delft3d]`
  only, and a new `comparison_unavailable` caveat is present. Reworded `ui_text.json`'s
  `onboarding.noPairedSph` to name the actual state ("D-Flow run only. The Teesta SPH attempt (a02)
  completed... results are under investigation"). Verified live in the browser: the Compare page
  shows this message and no fabricated IoU/F1/RMSE numbers. Added
  `tests/m4_sph/test_compare_mvp.py` (2 tests: the gate fires and produces a schema-valid
  `available:false` response; without the exclusion caveat the existing grid-dependent path is
  reached instead, confirming the gate — not a missing-fixture accident — is what short-circuits).
- **Item 2b (recommended, done):** found and fixed the actual cause of the reported 259.5 m SPH
  depth. `MeasureTool -elevation` does not preserve the request order of `POINTSENDLIST` columns —
  it re-sorts them by position (x ascending, then y ascending within x) — but
  `backend/m4_sph/measuretool.py`'s `parse_elevation_csv` assumed request order, so every
  near-field cell's depth search was silently paired with a *different* cell's free-surface
  series. Fixed by reordering output columns using the CSV's own `PosX [m]:`/`PosY [m]:` header
  rows against the request list (nearest-point match within 1 mm, each output column claimed at
  most once; raises rather than guessing on any mismatch). `-vars:vel` explicit-point output was
  checked against the real a02 raw data and does preserve request order — only the elevation path
  needed the fix. Added a regression test with deliberately shuffled header columns, plus an
  unmatched-point rejection test; the existing real-binary test (`DSPH_BIN_DIR` set) still passes.
  Re-ran post-processing only (`backend.m4_sph.postprocess`, no solver rerun) on the retained a02
  particle data: max depth corrected from **259.5 m to 11.47 m** (now under the 11.8 m inlet
  ceiling; the `sph_depth_search_capped` caveat is gone), max velocity from 3.0 to 3.29 m/s. Only
  5 of 3,600 cells are wet — consistent with the still-unresolved defect (b) from the prior
  session (the run loses ~99% of its fluid to the >100% particle-exclusion warning almost
  immediately), so `sph_particle_exclusion_warning` remains and Compare correctly stays
  unavailable regardless of this fix. This is a genuine bug fix to the postprocessor, kept
  separate from item 2's gate, which alone is what actually protects the dashboard.
- Tests: `tests/m4_sph` under `DSPH_BIN_DIR` (real MeasureTool binary), full suite: **80 passed**.
  `tests/m4_sph tests/m0_api` together: **259 passed**. Frontend `tsc --noEmit` passes; the Compare
  page was checked live in the browser (screenshot, not committed) and matches the intended
  "D-Flow run only" state with no fabricated metrics.

## 2026-09-27 — Demo stabilization pass, item 3: observed-extent check (nothing to build)

- `data/teesta/observed/flood_extent_2023.geojson` does not exist, and no flood-extent GeoJSON
  exists anywhere under `data/` (only `data/teesta/gee/lake_latest.geojson`, an empty lake-outline
  FeatureCollection, which is not a flood extent). No IoU/F1 check was run because there is nothing
  to compare against.
- Verified live in the browser that Monitoring keeps its existing honest state: "No digitized
  event extent available" plus the two actual cached Sentinel-2 images
  (`sikkim_glof_2023 · pre`/`post`, 2023-09-26/2023-10-26). No code change was needed or made.

## 2026-09-28 — Demo stabilization pass, item 4: Rishi Ganga removed from the MVP

- `mocks.KNOWN_SITE_IDS` (`backend/m0_api/mocks.py`) claimed to match `sites/*.yaml`, but listed
  `"rishiganga"` even though no `sites/rishiganga.yaml` exists — the comment was already wrong.
  Every site-scoped endpoint that used it (`/sites/{id}`, `/gee/{id}`, `/compare/{id}`,
  `/validation/{id}`, `POST /flood/query`) was serving **contract-example payloads with fake
  scores** for a site that was never actually onboarded. Dropped `"rishiganga"` from
  `mocks.KNOWN_SITE_IDS` and the separate `worker.KNOWN_SITE_IDS` copy (kept in sync, matching the
  existing pattern); `_require_known_site` now 404s it with an explicit reason: "Rishi Ganga is not
  configured in this MVP: the 2021 Chamoli event was a rock-ice avalanche / mass flow, not a dam
  breach." `GET /sites` now filters the example fixture's base list down to entries
  `_require_known_site` would also accept, so a listed site never 404s when opened —
  `contracts/examples/site_list.example.json` itself is untouched (it's a schema reference
  fixture, not a live registry).
- Checked the frontend: it has no hardcoded Rishi Ganga/Chamoli copy anywhere (`grep -rn -i
  "rishi\|chamoli" frontend/src frontend/app` — no matches); the site selector is fully
  driven by `/sites`, so it disappears automatically once the backend stops listing it. No
  frontend change was needed.
- Verified via `TestClient`: `GET /sites` now returns only `["teesta"]`; `/sites/rishiganga`,
  `/gee/rishiganga`, `/compare/rishiganga`, `/validation/rishiganga`, and
  `POST /flood/query {site_id: rishiganga}` all return 404 with the reason above.
- Updated `tests/m0_api/test_endpoints.py`: `test_list_sites` now asserts Rishi Ganga is absent and
  every listed site opens successfully; the old
  `test_get_site_detail_other_site_is_patched_not_hardcoded` (which relied on Rishi Ganga being a
  second working site) is replaced with `test_get_site_detail_rishiganga_not_configured_with_reason`.
- Tests: `tests/m0_api`: **178 passed, 1 skipped**. Frontend `tsc --noEmit` passes.

## 2026-09-28 — Demo stabilization pass, item 5: demo walkthrough verification

- **Walkthrough path exercised with a throwaway Playwright script** (real mode, no mocks, not
  committed): home → "Open Teesta III Demo" → flood map → timeline playback → impact → compare →
  monitoring → exports → validation → data layers. **Zero console errors and zero failed/4xx/5xx
  requests** anywhere on the path.
- **Found and fixed a real bug along the way:** KML export (`GET /export/{query_id}?format=kml`)
  read only `extent["features"][0]["geometry"]["coordinates"][0]` — the outer ring of a single
  `Polygon`. The real Teesta extent is a `MultiPolygon` with 70 disjoint parts (closed depressions
  plus the main channel, per item 1a's DEM-pit finding); the old code silently exported only one
  part's ring as if it were a flat list of points, producing corrupted, 358-byte output. Replaced
  with `_extent_geojson_to_kml`, which walks every `Polygon`/`MultiPolygon` part and every hole
  (`innerBoundaryIs`). Verified against the real registered run: KML is now 81,226 bytes, 70
  `<Polygon>` elements, 1 hole — versus the previous 358 bytes. Added
  `test_export_kml_covers_every_part_of_a_multipolygon_extent_with_holes` (constructs a two-part
  MultiPolygon with a hole and asserts the exported KML is well-formed XML covering every part).
- **Wired the dashboard's "Downstream impact" preview to real data.** It previously always showed
  an empty list in real mode (it read the legacy screening model's synthetic exposure points, which
  the real Teesta path never populates) captioned "Imported exposure inventory" — an honest-looking
  but empty claim. It now shows the top of the real `impactData.warning_table` (Chungthang, 382 min)
  captioned "Direct solver exposure intersection" when `floodQuery.method === 'delft3d_direct'`.
- **3D terrain tab:** confirmed it silently stays on the 2D map when a real flood raster is loaded
  (`Tabs value={floodRasterLayer?'2d':mapMode}` overrides the clicked tab) rather than rendering a
  broken 3D view. This already keeps it out of the real-mode path with no misleading claim, so no
  further change was made; per the plan it stays excluded from the recorded walkthrough.
- **Dashboard's "Prepared for rapid decisions" mini-pipeline** ("0 scenarios", Terrain/Physics/
  Cache/Interpolate/Impact) is generic framing copy, not fabricated data (0 is the honest scenario
  cache count) — left as is.
- Visual suite: ran with real backend data: only `compare.png` changed (the intended `available:
  false` state from item 2); updated that snapshot only. Re-ran full suite after: **13 passed**.
  Frontend `tsc --noEmit` and `npm run build` pass.
- Tests: `tests/m0_api` (including the new export test): **179 passed, 1 skipped**.

## 2026-09-28 — Demo stabilization pass, item 6: housekeeping

- **One working Python environment for tests, confirmed:** the `tests/m3_dflowfm/conftest.py`
  fixture-import fix (committed in item 0) already resolves the `pytest_plugins` collection error.
  `.venv/bin/python -m pytest --collect-only -q` now collects **937 tests** with no environment
  activation needed (previously required the `sih26` conda env, which has `richdem` and other
  extras `.venv` lacks; both envs now collect the same count).
- **CLAUDE.md:** the M3 module row said `backend/m3_delft3d`; the real directory is
  `backend/m3_dflowfm`. Fixed the path only — no module or model-ID rename.
- **`docs/decisions.md`:** added a "superseded" banner to the 2026-09-26 "M3: ANUGA replaces
  Delft3D 4 FLOW" entry (previously had none, so it read as current), and extended the existing
  2026-09-25 "M3: Delft3D 4 FLOW, not FM" banner to note the further supersession by M3-B ("M3:
  back to Delft3D FM"). D-Flow FM was already correctly identified as the current M3 solver
  elsewhere in both `CLAUDE.md` and `docs/decisions.md`; only these two stale-looking entries
  needed a pointer forward.
- **`docs/Equations.md`:** the XZ9 §1.2 note ("`h_r` is the fixed Xu & Zhang model constant, 15.0
  m") had no citation, unlike the equivalent §2.3 note. Added "Source: PRIMARY (Xu & Zhang 2009),
  doi:10.1061/(asce)gt.1943-5606.0000162" to match.
- No code changed in this item; `pytest --collect-only` and a read-through of the edited doc
  sections were the verification.

## 2026-09-28 — Teesta MVP demo-stabilization pass: session summary

Goal: a demo-ready Teesta MVP for a recorded video. Stabilization only — fix, caveat or hide what
already existed; no new modules, no contract-version bumps, no new solver campaigns. Approved
scope was items 0, 1a, 2 (+2b), 3, 4, 5, 6; item 1b (a conditioned-DEM D-Flow rerun) and the
stretch real campaign were explicitly declined/deferred. Seven commits, one per item, each with
its own detailed entry above (`c050144`…`f2c1738`).

**What was built**
- `backend/m0_api/dem_diagnostics.py`: reuses the project's own priority-flood implementation
  (`backend.m1_terrain.hydro.route`) to classify a direct run's extreme cells as sitting in an
  unconditioned DEM depression and/or a steep reach, feeding two new honest caveats
  (`dem_depression_ponding`, `clear_water_steep_reach_velocity`) plus a `provenance.diagnostics`
  block — no contract version bump.
- POI grid-snapping detection in `real_impact.py` (flags POIs that are dry only because of 90 m
  grid resolution, not genuine non-exposure).
- A hard gate in `backend/m4_sph/compare_mvp.py`: no paired SPH/D-Flow metric is ever published
  when the SPH run's `run_meta` carries `sph_particle_exclusion_warning`.
- A real bug fix in `backend/m4_sph/measuretool.py`: `MeasureTool -elevation` re-sorts output
  columns by position instead of preserving request order; `parse_elevation_csv` now reorders by
  the CSV's own header. Re-postprocessing the retained SPH run (no solver rerun) corrected its
  reported max depth from 259.5 m to 11.47 m.
- Rishi Ganga removed from the served site list (`mocks.KNOWN_SITE_IDS`), with an explicit 404
  reason instead of contract-example fallback payloads.
- A real KML-export bug fix (`main.py`): the old code read only the first ring of the first
  feature, silently dropping 69 of 70 parts of the real Teesta extent's MultiPolygon and every
  hole. Fixed and verified against the real run (358 bytes → 81,226 bytes, 70 polygons, 1 hole).
- Frontend: `DirectRunMetrics` headline (flood extent + affected-POI peak depth/velocity, with the
  single-cell domain max demoted to a labelled artifact line), a real-data "Downstream impact"
  dashboard preview, and the new caveat/message strings in `ui_text.json`.
- Doc fixes: CLAUDE.md's M3 module path, two stale "superseded" banners in `docs/decisions.md`,
  and a missing citation in `docs/Equations.md`.

**Still stubbed or placeholder**
- The D-Flow FM headline extremes are caveated, not fixed — item 1b (conditioning the M1
  far-field DEM before mesh generation) was explicitly declined for this pass. Re-running with a
  conditioned bed would remove the ponding artifact; the steep-reach velocities would likely
  remain, since they are slope-driven.
- The SPH near-field run still loses about 99% of its fluid almost immediately after the inlet
  (an unfixed physics/case defect); Compare correctly stays unavailable regardless of the
  MeasureTool parser fix.
- No observed flood-extent GeoJSON exists, so there is no IoU/F1 validation check — Monitoring
  says so honestly rather than showing anything.
- Rishi Ganga has no site config at all (not even placeholders); it is out of this MVP entirely,
  not just hidden.
- `m5_emulator.loocv --site` is not implemented, so nothing here trains M5 on real runs; the site
  configuration still carries dozens of placeholder fields (unchanged by this pass).
- The 3D terrain tab silently stays on the 2D map once a real result is loaded rather than
  rendering an explicit "unavailable" state — acceptable because it makes no false claim, but not
  a polished state either.

**Next step**
Record the demo using the walkthrough path verified in item 5 (home → Teesta → flood map →
timeline → impact → compare → monitoring → exports), following the commands and checklist already
given to the user. If there's time before recording, the highest-value next investigation is
item 1b (DEM conditioning) — it's the one open item with a known fix and a bounded scope, and it
would remove the least-honest-looking number left on the dashboard (the 66.9 m domain max, even
though it's already caveated). After that, the SPH particle-exclusion defect (b) is the next
blocker to a real Compare page.

## 2026-09-28 — 3D view wired to the real Teesta MVP run

- **Backend check first, no backend change needed:** called `scene3d.build_scene()` directly
  against a live registered `delft3d_direct` query and confirmed it already returns real terrain
  (845×1672 @ ~90 m) and median flood-surface float32 arrays — 11.3 MB total, well under the 20 MB
  cap — with no fallback to the contract example. `comparison.delft3d_surface_url` and
  `sph_surfaces` are both already empty for this run (no `summary_nearfield/max_depth.tif` for the
  D-Flow run, and the SPH run id isn't even in this query's `provenance.run_ids`, consistent with
  `compare_mvp.py`'s particle-exclusion gate), so the "don't show unresolved SPH" rule was already
  satisfied by the data with no gating logic to add. Query ids are ephemeral — each demo click
  issues a fresh `POST /flood/query` — so there is no fixed id to hardcode; the frontend now reads
  `floodQuery.query_id` once a real query is loaded.
- **Frontend, the actual gap:** `api.scene3d()` existed but was never called; the 3D tab was
  force-switched to 2D whenever any raster layer (real or legacy) was loaded, and `Terrain3D` only
  ever received the legacy synthetic `Grid`/`Result` model.
  - `src/data/api.ts`: added a proper `Scene3DResponse` type (was untyped `request(...)`).
  - `src/data/source.ts`: added `getScene3d()` and `getScene3dArrays()` (fetches
    `terrain.bin`/`flood_surface.bin` as `float32_le_row_major`, per `contracts/scene3d.md`) —
    components still don't fetch directly, matching this file's existing rule.
  - `app/sentriq/app.tsx`: new effect fetches scene3d + both binaries whenever a real
    `delft3d_direct` `floodQuery` is set. The 3D tab is only left unforced for this path
    (`force2dRaster = floodRasterLayer && !realSceneReady`); the legacy/synthetic screening path's
    forced-2D behaviour is unchanged.
  - `app/sentriq/terrain-3d.tsx`: added a real-data branch (same Three.js/OrbitControls stack, no
    new library) alongside the untouched legacy branch. It crops to the bounding box of *wet*
    flood-surface cells (+ margin) so the reach that actually flooded — South Lhonak → Chungthang
    — is what the camera frames by construction, not the whole 845×1672 far-field DEM; subsamples
    to ~140 samples/axis (comparable vertex budget to the legacy synthetic mesh); applies the
    response's `vertical_exaggeration` to Z only; builds the single median water surface once (no
    animation — the contract has no time dimension here, unlike the legacy per-frame path). No
    near-field GLB loading was written: since `delft3d_surface_url`/`sph_surfaces` are always empty
    for this run today, that would have been untested, speculative code — the caption instead
    states "NEAR-FIELD SPH: UNDER INVESTIGATION" plainly. Confidence/caveat/placeholder badges
    needed no change: they already render in the scenario-inspector aside regardless of which map
    tab is active.
- **Verified live** (throwaway Playwright script, not committed): home → Open Teesta Demo →
  Simulation → 3D terrain tab. Zero console errors, zero failed requests; network showed
  `scene3d` (1.2 kB) + `terrain.bin`/`flood_surface.bin` (5.65 MB each, 11.3 MB total) all 200. The
  rendered scene shows real mountain topography with the actual flood channel traced in cyan
  through it — not a flat/empty block — confirming the wet-cell crop is centred on genuine solver
  output. `tsc --noEmit` and `vite build` pass; full Playwright visual suite: **13/13 unchanged**
  (the automated harness never opens a real query, so no baseline needed updating).
- **Still limited:** near-field comparison meshes remain unwired in the 3D view (dead code was
  avoided rather than added) — revisit once the SPH particle-exclusion defect is actually fixed and
  `sph_surfaces`/`delft3d_surface_url` start returning real assets. No backend files changed this
  session.

## 2026-09-28 — 3D view: water-surface fix and breach marker

**Diagnosis first, as asked, before any code changed.**

- **Water surface not visible — root cause was the client, not the payload.** Checked in order:
  (a) `GET /scene3d/{query_id}` already returned a real grid: source `depth_p50.tif` has 2,379 wet
  cells (>0.1 m, 611×623 @ 90 m); the backend's own resample onto the 845×1672 terrain-aligned grid
  preserved 3,125 (no loss). The backend wasn't the bottleneck. But its `Resampling.average` for
  the depth reprojection is fragile in general: a dry-but-in-domain cell is a real 0.0, not nodata
  (CLAUDE.md rule 8), so averaging a narrow channel with its many dry neighbours would dilute or
  erase it at any real downsample factor — hardened to `Resampling.max` (`backend/m0_api/scene3d.py`)
  regardless. (b) nodata handling in the renderer was already correct. (c)/(d) heights and material
  were already consistent (same `minZ`/`scaleY` as terrain; transparent, added after terrain). The
  actual, dominant cause: last session's own client-side crop+subsample step read the water grid at
  the exact nearest-index (row,col) sample point on a coarse ~140×140 lattice instead of scanning
  each destination block for any wet pixel — for this query's 536×528-cell crop (row/col step 4),
  that hit only 204 of 3,125 wet cells (~6.5%), which is why the channel was barely visible.
- **Fix (`frontend/app/sentriq/terrain-3d.tsx`):** the water-building loop now scans every source
  cell in each destination block for the max water-surface elevation (any-wet, not nearest/mean),
  so the channel survives subsampling. Also added, per the checklist: `depthWrite:false` +
  explicit `renderOrder` on the water group (defensive hardening for (d), not the actual cause), and
  a small local-frame-only epsilon lift on the water mesh (not a physical-metre change) to keep
  shallow edge cells from z-fighting the terrain now that far more of them render.

**Breach marker (item 2).**

- **Location:** `sites/teesta.yaml` `dams[south_lhonak].breach_location` = `[88.2, 27.905]` deg,
  `status: placeholder` (already labelled an unverified guess in the source file). `GET
  /sites/teesta` was serving the frozen contract example verbatim, never the real site config,
  and had no lon/lat anywhere; `key_specs` is an open dict of SourcedValues by contract (§5.1,
  `additionalProperties: {$ref: SourcedValue}`), so overlaying real `location`/`breach_location`
  there (`backend/m0_api/main.py`, `get_site()`) needed no schema change.
- **Values actually used to build this run:** found the real sidecar M2 already wrote at
  `data/teesta/breach/hydrographs/teesta_2023_mvp__south_lhonak.json` (matches `run_meta.json`'s own
  `forcing_provenance_path` field): `event_volume_m3` = 50,000,000 ("approximate event constraint
  supplied for MVP"), `peak_discharge_m3s` = 7,355 at Chungthang (explicitly a downstream
  reconstruction target, not source-node discharge), `duration_s` = 14,587.89 (~4.05 h, from
  `T=2V/(Qpeak-Qbase)`). No `breach_params.json` exists anywhere for teesta — M2's dual-method
  width/failure-time was never written to disk for this site — so the tooltip honestly shows
  "Breach width: not computed", never a guessed number.
- **Serving these real artifacts:** `run_meta.json` and the breach hydrograph provenance JSON
  weren't reachable through any endpoint. Added three narrowly-scoped, read-only path patterns to
  the existing generic `/api/v1/files/{path}` passthrough (`backend/m0_api/main.py`) — the same
  mechanism already serving scene3d/timeline/compare/GEE assets, never schema-validated, so this is
  not a contract change. `breach_params.json` (genuinely, usually absent) returns `{"available":
  false}` with 200, not 404 — matching the existing Compare-page convention for an expected-missing
  artifact, so the walkthrough stays free of logged 4xx noise for a state that isn't an error.
- **Placement:** converts `breach_location` lon/lat to the scene's UTM frame with a small,
  self-contained closed-form WGS84 Transverse Mercator function (`frontend/lib/utm.ts`) — verified
  against `pyproj` to ~2 cm — rather than adding a projection library or touching the locked, fully
  closed `scene3d.schema.json` (`additionalProperties: false` throughout) to carry pre-projected
  coordinates.
- **UI:** a small red sphere + stem marker at the breach point (elevation sampled from the real
  terrain array, so it sits on the surface); hover or click raycasts against it and toggles a
  `CSS2DObject` tooltip (three.js's own bundled addon — not a new library) showing the four values
  above, titled "Breach location", captioned with the existing caveat language, all new strings
  added to `src/content/ui_text.json` under a new `scene3d` key per STYLE_GUIDE.md. Learned the hard
  way that `CSS2DRenderer` overwrites `element.style.transform`/`display` every frame from its own
  `object.visible`/`object.center` — toggling visibility must go through `CSS2DObject.visible`, and
  anchoring must go through `.center`, not inline CSS, and `.s-notice`'s `display:flex` (built for
  one icon+text row) breaks a stacked multi-line card, so the tooltip reuses `.s-notice.warning`'s
  colours/radius/border values directly rather than the class itself.
- **2D map marker — scoped out.** Checked: it would not have been a small change. `TerrainMap`'s
  existing pin placement (`percent(lon,lat)`) is built entirely on the legacy `grid` bbox
  (west/east/north/south), and `grid` is always `null` on the real MVP path (`source.getTerrain()`
  intentionally never resolves it) — there's no coordinate system for a pin to attach to without a
  real rework of that placement math. Left for a future session; the instruction's own phrasing
  ("if it's a small change") anticipated this.
- **Verified live:** water is now a continuous, correctly-shaped channel along the real South
  Lhonak → Chungthang reach; the red marker sits exactly at its head; hovering shows the tooltip
  with the values above, matching the real artifacts byte-for-byte. Zero console errors, zero
  failed/4xx requests on the full path (home → Teesta demo → 3D tab → hover). `tsc --noEmit` and
  backend `py_compile` clean; `.venv/bin/pytest -q tests/m0_api`: **179 passed, 1 skipped** (no
  regressions); full Playwright visual suite: **13/13 unchanged** (no baseline touches the 3D tab).

## 2026-09-28 — Task E: Compare Models tab, re-verified (no defect found)

- **Reproduce/report (item 1):** the tab is not broken. `GET /api/v1/compare/teesta?scenario_id=teesta_2023_mvp`
  returns 200 with a schema-valid, honest `available:false` payload; the frontend
  (`frontend/app/sentriq/app.tsx`'s `view==='compare'` block) renders it correctly. Live check (throwaway
  Playwright script, not committed): home → Open Teesta Demo → Compare Models — **zero console errors,
  zero failed/4xx requests**. Screenshot confirms: D-Flow FM panel populated, SPH panel shows "SPH vs
  Delft3D comparison is unavailable... The Teesta SPH attempt (a02) completed, but its particle-exclusion
  warning and anomalous depth field mean its results are under investigation and are not shown here," the
  `whenToUse` fine-print line ("Use SPH for near-field surge details and Delft3D for larger downstream
  domains, subject to matching inputs and validation"), run IDs, and all three caveats
  (`direct_solver_output`, `mvp_reconstructed_forcing`, `comparison_unavailable`). This is exactly the
  "SPH run listed as completed/under investigation, no diff metrics" fallback the task asked for.
- **Item 2 (the 259.5 m / >100%-exclusion anomaly):** already root-caused and handled in the
  2026-09-27 sessions above, before this one started — not rediscovered here, just confirmed still true.
  The 259.5 m figure was a `MeasureTool` column-reordering bug in `parse_elevation_csv`, fixed then
  (corrected max depth 259.5 m → 11.47 m, under the 11.8 m inlet ceiling). The underlying
  `sph_particle_exclusion_warning` (SPH run loses ~99% of its fluid at the inlet almost immediately) is a
  genuine solver/case defect, still unfixed, and `backend/m4_sph/compare_mvp.py`'s hard gate
  (`_unavailable_comparison`) correctly keeps Compare at `available:false` because of it — so item 2's
  "no diff metrics" fallback is the correct, intentional state, not a regression to chase.
- **Item 3 (side-by-side layout, diff layers, metrics, when-to-use line):** the available-branch code
  path (KPIs, layer URLs, probe table, `whenToUse` copy) already exists in `app.tsx` and is exercised by
  `tests/m4_sph/test_compare_mvp.py`'s non-gated case; it isn't reachable for Teesta today only because
  the SPH run is genuinely gated. No changes were needed or made to reach the required end state.
- **No code changes this session.** Created branch `mvp/compare-models` per the task-ownership convention;
  nothing to commit on it. Ran the full relevant backend suite fresh: `.venv/bin/pytest -q tests/m4_sph
  tests/m0_api`: **252 passed, 8 skipped** (the 8 skips are all `DSPH_BIN_DIR`-gated real-binary tests,
  expected without the binaries set) — no regressions from the 3D-view/breach-marker work done since the
  gate was added. `npx tsc --noEmit` in `frontend/` is clean.
- **Still limited (same as before):** the SPH near-field particle-exclusion defect itself remains unfixed;
  a real Teesta SPH-vs-Delft3D comparison with metrics is not possible until that solver/case issue is
  resolved and a clean SPH run is registered.
- **Next step:** if a real paired comparison is wanted for the demo, the next owner needs to either fix
  the DualSPHysics inlet/boundary setup causing the particle exclusion, or accept the current
  "D-Flow only, SPH under investigation" state as the honest MVP answer for this tab.

## 2026-09-28 — Validation tab: honest content instead of a raw JSON dump

Task D. Branch `mvp/validation` (created fresh off `main`; `mvp/exports`/`mvp/2d-map-satellite` were at
the same commit, so this doesn't fork off another task's in-progress work).

- **Diagnosis first.** `GET /validation/{site_id}[?event=]` (`backend/m0_api/main.py`) was already honest
  for real-run sites (no fabricated LOOCV numbers), but had nothing real to report: no M5 LOOCV was ever
  trained/saved for Teesta, and no observed 2023 flood extent was ever digitized via
  `backend.m7_gee.observed`. The frontend (`frontend/app/sentriq/app.tsx`) never even called the
  `?event=` branch — `source.getHistoricalValidation` didn't exist — and its only "success" rendering was
  `<pre>{JSON.stringify(validationData.summary)}</pre>`, plus it borrowed Compare-tab loading/error copy.
- **Built**, all in `backend/m0_api/validation_helpers.py` (new, owned module; `main.py`'s `get_validation`
  handler — the only route I touched — now calls it):
  - `observed_extent_status`: looks for `data/<site>/gee/observed/<event>_observed.geojson`; if absent,
    says so plainly instead of inventing IoU/F1 (item 2a from the task).
  - `build_literature_comparison`: reads this run's own `timeseries.csv` at the Chungthang POI (peak
    depth/velocity, arrival `s_since_t0`), derives absolute t0 from the breach hydrograph's own
    `peak_time_ist - duration_s/2` construction formula, and compares against the Sikkim-flood and
    South-Lhonak-GLOF reconstruction citations already in `docs/data_sources.md` (src_044/045) — labelled
    throughout as "comparison with other model reconstructions, not observations" (item 2b). Peak
    discharge (m³/s) is explicitly reported as not comparable: this run's POI output has depth/velocity/
    WSE only, no cross-section discharge, and the 7355 m³/s src_045 figure was already this run's own
    upstream forcing target, so it can't be an independent check either way — confirmed with the user
    rather than guessing a channel width to force a number.
  - `build_predicted_extent`: a real computed value (not invented) — flooded area from this run's own
    `summary/max_depth.tif` at its own `thresholds.extent_m`, returned as a proper low-confidence
    `predicted` Estimate.
  - `synthetic_loocv_summary`: surfaces `reports/m5_synthetic/validation/loocv.json` (the M5
    acceptance-test synthetic world), clearly labelled `"world": "synthetic_test_world"` and explicitly
    "not a Teesta-specific validation" — path overridable via `SIH26_M5_SYNTHETIC_LOOCV_REPORT` for tests
    (item 2c).
  - The real event id is read from the loaded site config (`sites/teesta.yaml` declares
    `sikkim_glof_2023`, not the contract example's generic `teesta_2023`) and now returned in the plain
    `/validation/{site_id}` response's `events` field, so the frontend can discover it generically instead
    of hardcoding a site-specific event id.
- **Frontend** (`app.tsx`, `frontend/src/data/api.ts` + `source.ts`, `frontend/src/content/ui_text.json`):
  wired `getHistoricalValidation`, replaced the borrowed Compare-tab loading/error strings with
  validation-specific ones, kept the existing raw-JSON-dump pattern for the LOOCV summary (matches the
  file's established style, `frontend/CLAUDE.md`'s "reuse existing components"), and added a literature
  comparison table plus observed/caveat notices using the existing `Table`/`Empty`/`Badge`/`s-notice`
  components — no new UI library, no visual redesign.
- **Verified.** `.venv/bin/pytest -q tests/m0_api/test_validation_helpers.py tests/m0_api/test_endpoints.py
  -k validation`: **20 passed** (12 new unit tests for the helper module, 6 new + 2 existing endpoint
  tests, including the real Chungthang-timeseries/hydrograph fixture case and the observed-extent-present
  case). `npx tsc --noEmit` clean. Full Playwright visual suite: **13/13 passed** after intentionally
  updating only `validation.png` (the panel is taller now); confirmed against a live backend + real Teesta
  data that the extra content is real (arrival 2023-10-04T07:40 IST simulated vs. 00:30 IST cited, peak
  discharge rows correctly marked "not computed by this run"), not mock output.
- **Touched outside my ownership:** none knowingly — `get_validation` was already my assigned handler.
  While working, `backend/m0_api/main.py` was being concurrently edited in the same working tree by
  another session (Task A, `export_query` — shapefile/PDF export rework); my diff only touches
  `get_validation` and one new import line, but the file on disk mixes both changes since this isn't a
  git worktree. One pre-existing full-suite failure, `test_i1_synthetic_e2e.py::test_i1_demo_site_to_generated_artifacts`
  (`result.json` missing from the exported shapefile zip), is inside that concurrent work, not mine — left
  untouched.
- **Still limited:** no real M5 LOOCV library exists for Teesta (only the synthetic acceptance-test world,
  clearly labelled as such), and no observed 2023 flood extent has been digitized, so extent IoU/F1 still
  isn't possible — both are now stated honestly in the UI instead of silently absent.
- **Next step:** digitizing an observed extent via `backend.m7_gee.observed` and training a real per-site
  M5 library are the two remaining pieces that would make this tab a genuine validation, not just an
  honest "not yet validated" report.

## 2026-09-28 — Task A: exports (shapefile + PDF)

- **Reproduced on the registered real Teesta query** (`q_20260928T050632Z_ad87ba`, method
  `delft3d_direct`): all four formats returned 200, so nothing was crashing. The actual defects
  were content bugs. Shapefile: the zip was named `..._artifacts.zip` and bundled `extent.shp`
  (real geometry, real CRS) next to `result.json`, `extent.geojson` and the raw `layers/*.tif` --
  not the contract's shapefile deliverable (`extent`/`depth_classes`/`isochrones`/`pois_warning`
  layers, docs/handoff_contract.md §4.7), and the one polygon it did have carried only
  `source_run_id`/`synthetic` -- none of the `zone/conf/method/dep_p50/.../caveats` attributes the
  contract's field table names. PDF: `mock_files.text_report_pdf()` writes its report text inside a
  `%`-prefixed PDF comment (`main.py`'s old export route, line ~690 pre-fix) -- no viewer renders a
  PDF comment, so the export was a syntactically valid but visually blank 1-page PDF.
- **Fix (`backend/m6_impact/exports.py`, new module; `backend/m0_api/main.py`'s export route now
  calls into it):**
  - `build_shapefile_zip` -- a real `extent.shp/.shx/.dbf/.prj/.cpg` (fields: `zone, dep_p50,
    vel_p50, dep_class, conf, method, site_id, query_id, run_id, has_ph, caveats`, all <=10 chars
    per the contract's field table) plus `pois_warning.*` (points, one per `warning_table` entry
    with a resolvable POI location from `sites/<id>.yaml`). Verified with `geopandas` + `ogrinfo`:
    correct EPSG:4326 CRS, feature count and area match `extent.geojson`/`result.json`'s
    `inundated_area_m2` exactly. `depth_classes`/`isochrones` vector layers are **not** included --
    their thresholds (`docs/impact_outputs.md` "depth_classes_m"/"arrival_bands_min") are still a
    team draft, not yet in any `config/*.yaml`, so this stays out rather than guessing at them; the
    bundle's own `README.txt` says so explicitly instead of silently dropping the layers.
  - KML: moved the existing MultiPolygon+holes fix into the same module, added a real `<Style>`
    pulled from `contracts/styles.json`'s `extent_class.{high,possible}` fill/opacity (so the KML
    never invents its own colours), a popup `<description>` with the real max depth/velocity/caveats,
    and a `pois_warning` Folder of styled points. Verified: well-formed KML 2.2, opens with GDAL's
    `LIBKML` driver (`ogrinfo`).
  - PDF: `build_pdf_report` -- a real multi-section report built with matplotlib's PDF backend (no
    dedicated PDF library was in `environment.yml`; added `matplotlib`/`pillow`/`pypdf`, the last
    test-only for text extraction). Header (site name, query id, report label, generated timestamp),
    an embedded depth map (reused `rendering.render_and_cache`, so the export never re-implements
    styling), a key-numbers table, impact summary + warning table (or an explicit "not available"
    when `impact.json` doesn't exist), a caveats list with real human-readable labels (a small
    `CAVEAT_LABELS` dict for the caveat ids this route can see today; unknown ids fall back to their
    own id, title-cased -- never invented detail), provenance, and TerraFlow branding. Verified with
    `pypdf` (text extraction: real numbers/caveats/provenance present) and `pymupdf` (rendered page 1
    to PNG and looked at it -- see session transcript; not committed).
  - Found and fixed a real map-projection bug while building the PDF's map panel: `real_query.py`'s
    `_bounds_latlng` stores `[[west_lon, south_lat], [east_lon, north_lat]]`, but the contract
    documents `bounds_latlng` as lat-first (§1.3). For Teesta both coordinates happen to be <90 in
    magnitude, so a magnitude-based order guess silently produced a transposed map on the first pass
    (caught by rendering the PDF to PNG and looking at it, not by a schema). Fixed by trusting the
    known `real_query.py` order instead of guessing (`main.py:_bounds_lonlat`) -- left a comment
    flagging that a future `bounds_latlng` producer for another mode/site would need the same fix;
    did not touch `real_query.py` itself (outside this task's export-code scope).
- **Tests added** (`tests/m6_impact/test_exports.py`, 26 cases; also updated
  `tests/m0_api/test_i1_synthetic_e2e.py`'s export assertions for the new bundle contents/PDF
  extraction): depth-class bucketing edges, shapefile completeness (`testzip()`, all sidecars, CRS,
  attribute values, <=10-char field names), shapefile without resolvable POI locations skips the
  `pois_warning` layer rather than writing empty geometries, KML MultiPolygon+holes (carried over)
  and style/description presence, PDF non-empty + real text/numbers/caveats present, PDF with
  `impact=None` says "not available" rather than omitting the section, PDF without a map still
  renders a real report. `pytest -q tests/m0_api tests/m6_impact`: 268 passed (one
  `tests/m6_impact/test_loss.py` failure was pre-existing/order-dependent -- passes alone, unrelated
  to this session's files; not investigated further, out of this task's scope).
- **Still limited:** `depth_classes`/`isochrones` shapefile layers remain unbuilt (see above --
  blocked on the team settling `docs/impact_outputs.md`'s draft thresholds into config); `loss_inr`
  in the PDF's impact summary stays "not available" for a direct run, same as the API (M6 loss
  config's own placeholders, not an export bug).
- **Next step:** once `depth_classes_m`/`arrival_bands_min` land in `config/`, extend
  `build_shapefile_zip` with raster-to-polygon extraction for those two layers using the same
  `rasterio.features.shapes` approach `real_query.py` already uses for `extent`.

## 2026-09-28 — Task H: loss/damage sourced values (EUR->INR, price index, road width)

**Built:**
- Sourced all three placeholders in `config/impact.yaml` (`loss.eur_to_inr_2010`,
  `loss.price_index_2010_to_current`, `loss.default_road_width_m`), each `status: sourced`
  with a full citation, after presenting the figures for approval before writing them (per
  user instruction). New `docs/data_sources.md` entries `src_047` (RBI Handbook of Statistics,
  Table 139, EUR/INR calendar-2010 annual average: 60.6683), `src_050` (WPI All Commodities,
  two PIB releases + the official 2004-05->2011-12 linking factor, chained to
  `price_index_2010_to_current = 2.171`), `src_051` (IRC:73 carriageway widths;
  `default_road_width_m = 3.75` m, the single-lane figure, applied uniformly per the user's
  "single width now" decision — per-OSM-class widths stay an open item, §6).
  **Caveat carried into both the config and `src_047`/`src_050`:** RBI's site and several WPI
  archive pages are CAPTCHA-gated or raw .xls, so the FX rate was read via a secondary source
  (Wikipedia's RBI-Handbook-citing table) and the price index stops at Apr 2026 (not the true
  latest) because no reliable 2011-12->2022-23 WPI linking factor could be found — flagged for
  verification the same way `src_033`/`src_034` already are in this file, not silently trusted.
- Regenerated `data/teesta/exposure/asset_values.csv` via
  `python -m backend.m6_impact.jrc_damage teesta <xlsx>` — `value_inr_per_unit` now populated
  and `status: sourced` for all six JRC classes.
- **Found the JRC loss method (`backend/m6_impact/loss.py`, previously only unit-tested) was
  never wired into any live endpoint.** The real Teesta demo query is exclusively
  `delft3d_direct` (single deterministic D-Flow FM run, confirmed by checking every persisted
  `data/teesta/queries/*/result.json`), served by `backend/m0_api/real_impact.py`, which had a
  hardcoded null `loss_inr` — not because of the placeholders alone, but because a single run
  has no P10/P50/P90 ensemble to range over (`docs/impact_outputs.md` §5.1 already documented
  this as a second, independent blocker). Wired a new `_direct_run_loss()` helper in
  `real_impact.py` that reuses `backend.m6_impact.loss.building_losses`/`road_losses` against
  the run's single depth map and reports a **point estimate** (`interval: "none"`, no
  low/high) rather than inventing a P10-P90 spread from one run — the same choice this module
  already made for `population_persons`. `placeholder_fields`/caveats are now derived from
  which asset classes actually priced, not a hardcoded list.
- Frontend: added a "Loss by asset class" table + JRC-method badge + assumptions list to the
  Impact view (`frontend/app/sentriq/app.tsx`, `frontend/src/content/ui_text.json`,
  `frontend/src/data/api.ts` — extended `ImpactResponse.loss_inr` with optional
  `by_asset_class`/`assumptions`), reusing the existing `Table`/`Badge`/`Empty` components per
  `frontend/CLAUDE.md`.

**Verified:**
- `pytest -q tests/m6_impact` (excluding the pre-existing, unrelated `pypdf`-missing
  `test_exports.py`): 47 passed. Three tests assumed the ambient real config shipped
  placeholder FX/index/road-width and needed updating to force that state explicitly via
  `_config()`/local overrides now that the real config is sourced
  (`tests/m6_impact/test_loss.py`, `tests/m6_impact/test_jrc_damage.py`) — not a weakening of
  what they check, just decoupling "placeholder behaviour" tests from the ambient config state.
  `tests/m0_api` full run: 228 passed, 2 failed (`test_campaign_worker_runs_sph...`,
  `test_json_onboarding_runs_m1_m2_m5...`) — both pass individually and together in a 3-file
  rerun, confirmed pre-existing test-ordering flakiness unrelated to these changes, not
  investigated further (out of this task's scope). One more failure
  (`test_i1_synthetic_e2e.py`) is the same pre-existing `pypdf` gap as `test_exports.py`.
- Computed real loss numbers for a live Teesta `delft3d_direct` query directly (bypassing the
  stale running server, see below): ~₹1.08B total, residential ₹1.04B, roads ₹35M, commercial
  ₹2.1M, industrial ₹0 (no industrial buildings wet this run) — validated against
  `impact.schema.json`. `tsc --noEmit` clean; Playwright screenshot pass across all 12 screens:
  0 console errors.
- **Could not verify the new UI against the live browser demo** — the shared uvicorn server on
  port 8000 (PID 838171, conda env `sih26`, started 06:15, no `--reload`) is running from
  before these edits and won't pick them up; a screenshot through it still shows the old
  hardcoded null-loss text. Did not restart it unilaterally since another session or the user
  may be using it for demo recording — **needs a manual restart** (or explicit go-ahead to
  restart it) before the by-asset-class table is visible in the browser.

**Still limited:**
- `default_road_width_m` is one width for every OSM `highway=*` tag, not per IRC road category
  (open item, `docs/impact_outputs.md` §6) — the user explicitly chose this scope for now.
- The EUR/INR figure and the WPI->current chain both carry documented verification caveats
  (see `src_047`/`src_050`); re-derive the price index once an official 2011-12->2022-23 WPI
  linking factor is published.
- Facilities (hospitals/schools/bridges) and agriculture remain unpriced (no footprint/cropland
  layer) — unchanged from before, named in every result's `assumptions`.

**Next:** restart the demo server to confirm the by-asset-class table renders correctly live;
re-derive `price_index_2010_to_current` once a 2011-12->2022-23 WPI linking factor exists.

## 2026-09-28 — Task G: live GEE monitoring for Teesta + Monitoring page wiring

**Built**
- `earthengine authenticate` had never been run on this machine; walked the user through it live.
  `.env`'s existing `GEE_SERVICE_ACCOUNT_EMAIL`/`GEE_SERVICE_ACCOUNT_KEY` turned out to be a personal
  Gmail + a Google Maps-style `AIzaSy...` API key, not a service-account email + JSON key path (what
  `scene_search.gee_service_account_credentials()` actually reads via `GEE_SERVICE_ACCOUNT_KEY_PATH`)
  — left as-is since replacing them wasn't asked for and the personal-OAuth path (the one that ended
  up working) doesn't use them at all. `ee.Initialize()` still needed an explicit Cloud project (no
  server-side default for this account); the user supplied `sih-161`. Added `GEE_PROJECT=sih-161` to
  `.env` and a same-named fallback in `scene_search._ee_initialize()` (`backend/m7_gee/scene_search.py`)
  so both the CLI (`--ee-project`) and `POST /gee/{site}/refresh` (which never passes a project
  explicitly) pick it up without a code change at every call site.
- Ran `python -m backend.m7_gee.fetch teesta --months 24` live. First run only covered Oct
  2024-Sep 2026 — the container's clock reads 2026-09-28, so a trailing-24-month window never
  reaches back to the real Oct 2023 GLOF. Re-ran with `--months 37` to include it.
  **South Lhonak lake area: 2023-09-01 = 4,348,200 m², 2023-10-01 = 1,493,200 m²** — a ~66% drop,
  consistent with the real Oct 3-4 2023 outburst (the Oct row is a whole-month S2 composite, so it's
  already dominated by the post-drainage lake). `gee_meta.json` recorded `source: live` for both lake
  area and rainfall, zero fetch errors. `recheck.json` correctly reports `reason: no_trained_library`
  (teesta has no M5 emulator manifest yet, so there's nothing to compare the latest area against) —
  an honest state, not a bug.
- Monitoring page (`frontend/app/sentriq/app.tsx`, view `'monitoring'`) was already wired to real
  `GeeLayers` data (no mock fallback in real mode) with working status badges, recheck-outdated
  banner, refresh button, and imagery — but the observed-extent map panel was *always* the `Empty`
  placeholder (never actually rendered `lake_latest`), and lake area/rainfall were tables/lists only,
  not the "time series chart" the task asked for.
  - Added a lake-area line chart and a rainfall bar chart using Recharts + the existing (installed
    but previously unused anywhere) `frontend/components/ui/chart.tsx` wrapper — no new dependency,
    kept the existing tables/lists below each chart for exact readout.
  - Added `LakeOutlineMap` (`frontend/app/sentriq/ui.tsx`) to render the real `lake_latest` polygon:
    a small self-contained component, not an edit to `terrain-map.tsx` (owned by task C). It scales
    to the polygon's *own* bounding box rather than a flood-simulation `Grid`, because `Grid` is
    `null` on the real monitoring path (no DEM/flood domain is loaded there) — same blocker last
    session hit for the 2D breach pin. Reuses `TerrainMap`'s existing CSS classes (`.terrain-map`,
    `.map-topline`, `.map-chip`, `.north`, `.observed-polygons`) so it looks native; explicitly
    captioned "not a satellite photo" since it draws only the vector outline, not real pixels — real
    satellite imagery (pre/post event RGB, already fetched and cached) is shown separately below it,
    and combining the two would need a new bounds-carrying field on the locked `GeeLayers` contract,
    which CLAUDE.md rule 1 says not to change silently.
- `frontend/CLAUDE.md` forbids new UI libraries, so both additions reuse what's already installed.

**Verified**
- `pytest -q tests/m7_gee tests/m0_api`: 297 passed, 1 skipped (pre-existing, unrelated
  `DSPH_BIN_DIR` skip) — but only after fixing a real regression the live credentials exposed:
  `tests/m0_api/test_endpoints.py::test_refresh_gee` had zero mocking and previously stayed fast
  only because this machine had no working EE credentials, so `_ee_initialize` failed fast and
  `refresh_gee` fell back to cache immediately. With real credentials now present, the same test
  triggered a real, slow live fetch + `live_render.render_event_rgb` image render inside what's
  meant to be an offline unit suite, and hung for 10+ minutes before being killed. Fixed by
  monkeypatching `scene_search._ee_initialize` to always raise in that test, matching how every
  other GEE-touching test in the repo (`test_scene_search.py`, `test_imagery.py`,
  `test_recheck_scheduling.py`) already isolates itself from ambient machine state.
- `tsc --noEmit`: clean.
- Playwright, `frontend/visual`: full 12-screen `shots`/`diff` against `main-baseline` (temporarily
  added `--disable-web-security` to `shots.mjs`'s launch args to get past this session's dev server
  running on port 5175, not the CORS-allowlisted 5173, then reverted it) — 11/12 screens
  byte-identical (`changedPct: 0`), only `monitoring` changed (31.09%, expected: real chart/outline
  content replacing the always-empty placeholder). Zero console errors, zero failed/4xx requests on
  every screen. Manually verified the Refresh button end to end: badge flips from "Cached satellite
  data" to "Live satellite data" and `fetched_at` updates after a real live refresh completes.

**Still limited**
- The lake outline is a geometry-accurate vector diagram, not the lake polygon overlaid on real
  satellite pixels — see above; doing that properly needs a contract change (a new bounds-carrying
  imagery field on `GeeLayers`) that's out of scope to make unilaterally.
- `.env`'s `GEE_SERVICE_ACCOUNT_EMAIL`/`GEE_SERVICE_ACCOUNT_KEY` are still not a usable service
  account (personal Gmail + Maps-style API key); live fetches on a machine without a prior
  `earthengine authenticate` session will still fail until someone sets up a real service account
  or repeats the interactive auth.
- Only Teesta was fetched/verified live this session; Rishi Ganga's monitoring path is unexercised
  against real Earth Engine (should work identically, same `fetch.run()` code path, untested).

## 2026-09-28 — SPH outlet-zone diagnosis: attempts a02 and a03, defect not fixed

Task B/E, `mvp/compare-models`. Follow-up to the Compare Models re-verification above.

- **Both attempts a02 and a03 failed** with a particle-exclusion defect. Excluded-particle counts:
  540,237 (a02) vs. 540,251 (a03) — effectively the same magnitude, confirming a03's change did not
  address the underlying cause.
- **100% of exclusions are `Motive=1`** (position-based removal) in both attempts — never velocity or
  density exclusions. They begin at t=85 s at (247, 236, 1567), which is the bottom corner of the
  inlet's own zone box, not anywhere near the new outlet zone added in a03.
- **Mechanism:** the inlet uses `inputtreatment=2` (remove fluid), and its zone box is 145 m x 11.8 m
  over sloped terrain. Fresh inflow keeps re-registering as inside that same zone and gets deleted by
  the inlet's own removal treatment.
- **The outlet zone (a03's change) was correct and is being kept**, but it did not fix the defect,
  since the exclusions never occurred near the outlet in the first place — stating this plainly so the
  outlet zone isn't mistaken for a fix that didn't work.
- **Next step:** attempt 3 (a thin-slab inlet, to stop the inlet zone box from re-catching its own
  outflow) is pending approval before running.

## 2026-09-28 — Task C: 2D map satellite basemap + breach marker (mvp/2d-map-satellite worktree)

Built in an isolated `git worktree` (`../SIH26-161-2d-map`), not the shared main checkout — another
session was actively switching branches under the shared working directory mid-session (caught via
`git worktree list`/`git status` showing a branch change I hadn't made), so editing there risked
stepping on it. Findings/diff below are from that worktree; branch `mvp/2d-map-satellite`, not yet
merged.

**Read first:** the assigned task's premise ("the red breach dot is hidden behind the top-left
tab/panel" in the 2D map) didn't match reality — the last session's own entry above says the 2D
marker was explicitly **scoped out**, and grepping confirmed no breach marker existed anywhere in
`terrain-map.tsx`. Also: this app has no Leaflet anywhere (`grep -r leaflet` — nothing, not in
`package.json` either); the 2D map is a hand-built canvas/percent-positioned component
(`app/terrain-map.tsx`), despite root `CLAUDE.md`'s stack line naming Leaflet. Built on the real
component instead of introducing a new mapping library (`frontend/CLAUDE.md`: "Do not add UI
libraries").

**Asked before building** (AskUserQuestion): the task's satellite-basemap item asked for a live
Esri/EOX tile fetch for the wide view, which conflicts with `CLAUDE.md` rule 11 ("Localhost only...
GEE only for offline prep, with cache + screenshot fallback") — exactly the pattern already built
for the cached Sentinel-2 imagery. Chosen: cached imagery by default, plus an explicit "Live imagery
(needs internet)" toggle, off by default.

- **Basemap.** `ContractRasterMap` (the component the real MVP path actually renders — `grid` is
  always `null` there, same as last session found) now shows the cached GEE Sentinel-2 imagery
  (`getObserved`/`data/teesta/gee/imagery/manifest.json`, already fetched app-wide via `geeData`)
  as a percent-positioned image overlay, clipped/positioned against its own real `bounds_latlng`
  nested inside the raster's bounds — same linear-percent technique the app already uses for
  exposure dots/observed polygons. A "Live imagery (needs internet)" toggle (off by default) instead
  fetches a single warped PNG from ArcGIS's public World Imagery `export` REST endpoint, sized to
  the query bounds — ambulatory, not the frontend's default. Both show attribution + acquisition
  date (or "live · date varies by area") in the existing `.map-bottom` strip. New "Satellite
  basemap"/"Live imagery" toggles reuse the existing `.map-layer-menu`/`Switch` pattern.
- **Breach marker, now unblocked.** Last session scoped the 2D marker out because the legacy `grid`
  bbox is arbitrary/unrelated to real lon/lat on the real MVP path. That's still true for `grid`,
  but `ContractRasterMap`'s `overlay.boundsLatLng` (from the same real query) is a real EPSG:4326
  extent — so the same `breachMarker` data already computed for the 3D view (`app.tsx`,
  extended with a `lonLat` field alongside its existing `utmX/utmY`) now also places a red dot
  (`#e5453f`, matching the 3D marker's colour) via percent-in-bounds math, with a click/hover
  tooltip reusing the exact same title/lines/caveat strings the 3D tooltip shows (`.s-notice.warning`
  colours inline, not the class — same reasoning the 3D session gave for why: that class's
  `display:flex` breaks a stacked multi-line card).
- **Layering fix.** Confirmed the "hidden behind the panel" bug *would* occur once a marker existed:
  `.map-topline`/`.map-tools`/`.depth-legend`/`.map-bottom` are plain `position:absolute` siblings
  with no `z-index`, painted in DOM order — anything inside `.map-world` (which gets its own
  stacking context from the pan/zoom `transform`) paints as one unit *before* them, so a marker near
  a corner would sit under that chip. Fixed by rendering the marker in its own last-child sibling
  (`.marker-layer`, sharing `.map-world`'s transform so it still pans/zooms with the raster,
  `pointer-events:none` on the wrapper so it doesn't block dragging). No global CSS touched
  (`frontend/CLAUDE.md`: "Do not change global styles... without asking first") — verified with
  `document.elementFromPoint` at the marker's actual screen coordinates: resolves to the marker, not
  a chip, confirmed by DOM child order (`marker-layer` last).
- **Also added:** pan/zoom to `ContractRasterMap` (it had none before — only the legacy `grid` path
  did), reusing the existing zoom-in/out/reset/layers `.map-tools` buttons, so "check visible when
  zoomed" is a real, testable state.
- **Real bug found and fixed while building the overlay/marker math (not by me touching backend):**
  `backend/m0_api/main.py`'s own `_bounds_lonlat` docstring already documents that the real
  direct-solver path (`real_query.py:_bounds_latlng`, and `real_timeline.py`) emits
  `bounds_latlng` as `[[west_lon,south_lat],[east_lon,north_lat]]` instead of the contract's
  documented `[[south_lat,west_lon],[north_lat,east_lon]]` (§1.3) — every other path
  (`backend/shared/grid.py`, `m7_gee/imagery.py`, the emulator/fallback query paths) gets it right.
  That existing PDF-export workaround's own magnitude heuristic (`|v|<=90`) **can't actually
  disambiguate for this project**: Teesta/Rishi Ganga longitudes (~74–95°E) are themselves ≤90, the
  same range as any latitude, so the heuristic silently mis-detects for exactly this project's real
  coordinates (confirmed by hand-tracing both branches of `_bounds_lonlat` against the real Teesta
  numbers). Before this was caught, my new overlay/marker math silently landed at the wrong axes
  (`No cached satellite imagery covers this extent`, marker off the visible map) — the raster
  `<img>` itself was unaffected only because it's stretched to fill its container regardless of what
  the bounds field says, so this bug was latent until something actually needed the field for real
  geometry. **Fixed on the frontend, not the shared backend file** (out of Task C's ownership, and
  `real_query.py`/`real_timeline.py` are shared with other in-flight tasks): `app.tsx` threads
  `floodQuery.method==='delft3d_direct'` down as `lonFirstBounds`, and `ContractRasterMap` un-swaps
  deterministically from that definitive signal instead of guessing from magnitude. **Backend
  owner: `_bounds_lonlat` (`backend/m0_api/main.py`) and any other magnitude-based bounds-order
  guess should be reviewed — it's provably wrong for this project's own longitudes**, not just an
  edge case.
- **Verified live** (Teesta demo, real backend on :8000, worktree dev server + `--disable-web-security`
  Chromium since backend CORS only allows `:5173`, occupied by another session): satellite imagery
  now renders precisely along the real flood channel with no visible offset at the imagery's
  Chungthang-area coverage; the breach dot renders correctly, confirmed on top via
  `elementFromPoint`; zero console errors on the full demo path. `tsc --noEmit` clean. `eslint`:
  +4 problems worktree-wide (3285→3289; 2 new `any` params, 2 new `<img>` LCP warnings), consistent
  with this codebase's existing style, not a new pattern. Playwright visual harness run against the
  worktree showed large diffs vs. a `:5173` baseline on every data-driven screen — confirmed to be
  the CORS-driven empty state (`home`, which needs no backend call, diffed only 0.45%), not a real
  regression.
- **Known gap:** no "terrain" basemap option (the task asked for satellite / terrain / none) — the
  real MVP path has no hillshade/terrain PNG artifact anywhere (only raw `dem.tif`); rendering one
  is M1/M0 backend work outside this task's frontend ownership. Left `basemap` as `none | cached |
  live` only.

## 2026-09-28 — Task F: offline mode (save-for-offline + app-shell service worker)

Offline caching was removed during the prototype strip (Group 2) and the two offline buttons had
been left permanently disabled ever since. Built it for real against the frontend/CLAUDE.md rule
of touching only the data layer: a new `frontend/src/offline/` module (`cache-store.ts` — Cache
Storage snapshot + localStorage manifest + size estimate; `resource-list.ts` — pure function
listing every GET URL a loaded query touches), a hand-written service worker
(`frontend/public/offline-worker.js`, no new dependency — this repo has no vite-plugin-pwa), and a
thin hook in `src/data/source.ts` (`initOffline`, `saveQueryForOffline`, `estimateOfflineSaveSize`,
`loadSavedOfflineQuery`, `listSavedOfflineQueries`) — components still never fetch directly.

- **Design:** the service worker's only job is app-shell caching (it precaches `/` plus that page's
  own `<script>`/`<link>` asset URLs during `install`, since there's no build manifest to read, and
  falls back to the cached response — any cache, shell or data — on a failed GET; it never touches
  POST). "Save for offline" is separate: it fetches every GET resource behind the *currently loaded*
  query (site detail, flood/impact/compare/validation/gee JSON, layer PNGs, every timeline frame,
  scene3d terrain+flood binaries, satellite imagery, all four export formats, plus the 3D breach
  marker's run_meta/forcing/breach_params files) directly into a separate Cache Storage bucket via
  the page itself, and records a small manifest entry (site, query, size, timestamp) in localStorage.
  On a cold boot while `navigator.onLine` is false, the app now loads the latest saved query
  straight from that cache (`GET /flood/{id}`, never a fresh `POST /flood/query`, which would mint
  an uncached id) instead of running the normal live boot sequence.
- **Wired in `app.tsx`:** the two previously-disabled buttons (library toolbar "Download for
  offline", settings "Prepare offline workspace") now call the same save handler, disabled when
  there's no loaded query or when already offline (can't fetch a fresh copy without a connection);
  new simulation runs, the Teesta demo button, GEE refresh, and Add-a-dam's scenario generation are
  now `disabled` (with a `title` reason and a standing `s-notice` banner) whenever `offline` is
  true; the header/dashboard badge shows "Offline — cached `<timestamp>`" once a saved query is
  reopened, instead of the generic "Offline workspace" string.
- **Verified for real** with a throwaway Playwright script (not committed, matching this project's
  established pattern): built the frontend against the real backend (no mocks), opened the real
  Teesta MVP run, waited for impact/timeline/compare/gee/scene3d to actually finish loading (the
  real `/impact` and `/flood/{id}/timeline` endpoints took ~14s and ~70s against this registered
  run — not a bug, just how slow the mock/real backend's own computation is), clicked "Save for
  offline" (27–30 resources, ~14.7 MB), then flipped the same browser context offline
  (`context.setOffline(true)` — a *new* context has separate storage and was the wrong way to
  simulate "reopen while offline", a dead end this session ran into first) and confirmed: the
  "Offline — cached" badge appears, the map/3D canvas and timeline render, impact metrics render,
  and clicking "Export selected format" produces a real download — all with **zero failed/4xx/5xx
  requests** once offline. Also ran `tsc --noEmit`, `vite build`, and `npm run check:shell` (all
  pass) against the built output.
- **Real bug found and fixed along the way:** the service worker's opportunistic runtime caching
  never actually caches the app shell in practice, because (a) a worker only controls the page that
  registered it from that page's *next* navigation onward — never the page that just registered it
  — and (b) this SPA never does a second full navigation (all internal routing is `pushState`), so
  the shell HTML/JS/CSS would never get cached by runtime interception alone. Fixed by having
  `install` explicitly fetch `/`, cache it, and parse its own asset URLs out of the markup to cache
  those too.
- **Still limited:** deleting a saved query only drops its manifest entry, not its Cache Storage
  bytes (best-effort only, documented in `cache-store.ts`); only the single most-recently-saved
  query is ever auto-reopened on a cold offline boot (this MVP only ever has one meaningfully
  distinct query per site); HEAD-based size estimation undercounts any endpoint that 405s on HEAD
  (this backend's FastAPI routes only implement GET) — those resources still get saved correctly,
  just without contributing to the pre-save size estimate shown to the user.

## 2026-09-28 — South Lhonak lake-area overestimate: NIR test, ISRO reference on Monitoring

**Tool access was broken almost this entire session:** `Bash` and the `Agent` subagent tool both
failed every call with a server-side "auto mode classifier gave no verdict" error, so nothing could
be run — no `pytest`, no `grep`/`find`, no live GEE fetch, no `tsc`, no Playwright. Everything below
was done read-only (the `Read`/`Edit` tools were unaffected) plus manual tracing of the logic; **none
of it has been run**. Whoever picks this up next should run `pytest -q tests/m7_gee`, `tsc --noEmit`,
and the Playwright visual suite before trusting it.

**Diagnosis:** the prior session's live Teesta fetch (2026-09-28, Task G) reported South Lhonak lake
area 4,348,200 m² (Sep 2023) / 1,493,200 m² (Oct 2023), about 2.6x ISRO/NRSC's published 167.4 ha /
60.3 ha (`src_072`, added to `docs/data_sources.md` this session). `fetch.py` already restricts each
month to the connected water-mask component containing the seeded dam location, so a separate
unrelated lake blob was not the cause; the likely cause is a bright-NIR (snow/ice) corridor whose
NDWI still clears the Otsu threshold, bridging the seeded lake to something else within the same
connected component. `GeeSettings.max_snow_ice_pct` only skips a month at >30% snow/ice over the
*whole* AOI, not a smaller connecting patch.

**Fix (`docs/decisions.md` 2026-09-28 "M7 GEE fetch: NIR test excludes snow from S2 water mask"):**
`provider.py` `s2_month` now also fetches B8 NIR reflectance; `lake_area.water_mask` gained optional
`nir`/`nir_max` and requires `nir <= nir_max` (new `GeeSettings.nir_reflectance_max = 0.15`) on top
of the NDWI test, S2 months only. `SyntheticProvider` gained a `snow_bridge_months` fixture that
reproduces the exact bridging failure mode; new tests in `tests/m7_gee/test_lake_area.py` (
`TestNirFiltersSnow`) and `tests/m7_gee/test_fetch.py`
(`test_snow_bridge_does_not_inflate_area_via_nir_filter`) cover it end to end — **unrun**, verified
only by hand-tracing the mask/component arithmetic.

**Not done:** a live re-fetch to report the actual corrected South Lhonak numbers — needs a working
Bash/backend session. Re-run `python -m backend.m7_gee.fetch teesta --months 37` and record the new
Sep/Oct 2023 rows in `docs/decisions.md`.

**Monitoring page:** `frontend/app/sentriq/app.tsx`'s lake-area panel now shows the ISRO/NRSC figures
(`ISRO_SOUTH_LHONAK_REFERENCE` constant, Teesta-only) as a static, sourced reference block below the
project's own series, captioned that it isn't used to calibrate thresholds — new `ui_text.json`
strings, reused existing `.s-panel`/`.dataset-row`/`.fine-print`/`SourceLink` styling, no new CSS, no
`GeeLayers` contract change (a fixed historical reference, not a live-fetched field). Visual/`tsc`
verification not run (see tool-access note above).

## 2026-09-28 — Merge all feature branches into main

Merged `mvp/gee-monitoring` (commits "v1": README, `.env.example`, `scripts/start.*`; "temp": ISRO
reference panel, `src_071`–`src_074`) into `mvp/final`, which already contained every other
`mvp/*` branch, then fast-forwarded `main`. `frontend-ui-shell` and `m0-job-system` were already
in `main`. The NIR snow-mask work in "temp" conflicted with `mvp/final`'s later version of the same
fix; `mvp/final`'s code and tests were kept, and the duplicated `decisions.md` entry was folded into
one. Also committed a pending `provider.py` fix from the `mvp/final` worktree (empty `reduceRegion`
cloud stat → treated as fully cloudy).

**Checks:** `pytest -q` on `main`: 1003 passed, 8 skipped, 1 failed, the same failure as before the merge
(`tests/m3_dflowfm/test_generator.py::test_pilot_reference_builder_reuses_frozen_geometry_fields_and_forcing`,
`assert 18037 == 18034`). `tsc --noEmit` clean. Playwright visual tests not re-run.

**Still limited:** `tests/data/breach_cases.csv` is gitignored (the `data/` pattern matches
`tests/data/`), so `test_breach_cases.py` errors in any fresh checkout or worktree. `docs/data_sources.md`
`src_071`/`src_073`/`src_074` still contain `<...>` fill-in placeholders.

## 2026-09-30 — Step 0: clean test baseline (missing-artifact gates only)

**Changed (tests/docs only; no product, simulation, emulator, site or frontend logic):**
- `tests/m2_breach/test_breach_cases.py`: the 7 `test_median_pe_in_paper_ballpark` cases now SKIP
  with a clear reason when `tests/data/breach_cases.csv` is absent, instead of erroring. The CSV is
  not committed (root `.gitignore` `data/` also matches `tests/data/`) and
  `scripts/build_breach_cases.py`, named in `docs/data_sources.md`, does not exist in the repo.
- `tests/m3_dflowfm/test_generator.py`: the pilot reproduction test now SKIPS when
  `backend/m3_pilot/inputs/export/` is absent (gitignored; regenerated by `make_export.py` from
  local `data/`). Where the export exists, the test runs unchanged and still reports the known
  open mesh mismatch (`assert 18037 == 18034`, `docs/m3_reproduction.md`).
- `CLAUDE.md`: fixed stale references (`docs/Equations.md`, `docs/m5_specs.md`,
  `sites/template.yaml`) and removed the nonexistent `backend/m3_cascade_pilot/`.

**Checks (fresh clone, no `data/`, no solvers):** before 991 passed / 1 failed / 7 errors /
13 skipped; after 991 passed / 0 failed / 0 errors / 21 skipped. Remaining skips are all
environment/artifact gates: DualSPHysics binaries (8, `DSPH_BIN_DIR`), D-Flow FM kernel or pilot
output (4), JRC workbook (1), breach case table (7), pilot export (1).

**Not changed:** `.gitignore`; XZ9 peak discharge stays blocked (`xz9.py` and its tests untouched).
**Open decisions:** commit `breach_cases.csv` (licence check) or add its build script; whether to
enable XZ9 peak discharge (g = 9.81 as in F16/F8) via a `docs/decisions.md` entry.

## 2026-09-30 — Feature 1: dynamic site onboarding & configuration

**Built:** `backend/m0_api/site_registry.py` + `config/sites.yaml` (decisions.md 2026-09-30).
`POST /sites` runs full `SiteConfig` validation (422 lists every invalid field), rejects duplicate
ids/names (409), persists the config, then queues the job (config rolled back if queuing fails).
`GET /sites`, `GET /sites/{id}` and `PUT /sites/{id}/recheck` are built from the saved config and
real job/emulator state for every site; `KNOWN_SITE_IDS` removed from `main.py`, `mocks.py`,
`worker.py` (rechecks now cover every registered site), `scene3d.py`, `timeline.py`.
`load_site_config` default lookup falls back to onboarded configs. Frontend: the Add-a-dam wizard
builds a real site config (`src/data/site-config.ts`; entered values are unverified placeholders
unless cited, blanks are null placeholders), POSTs it, polls `GET /jobs/{id}` and shows the real
stage/error; the DEM upload gate on submit is removed; `api.ts` now surfaces the server's error
message from FastAPI's `{detail: {error}}` envelope.

**Checks:** backend `pytest -q` green (new `tests/m0_api/test_site_onboarding.py`, lookup tests in
`tests/shared/test_site_config.py`); frontend `tsc`, `check:shell`, `test:offline`, new
`test:site-config` pass. Playwright snapshots could not be compared in the cloud sandbox (they
also fail on the unmodified commit there); regenerate/compare on the team machine.

**Not done (later features):** a new real site stops at `terrain_failed` until raw DEM/land-cover
inputs exist (Feature 2). Teesta-specific run/scenario paths in `real_query.py`,
`real_timeline.py`, `real_impact.py`, compare default scenario, and frontend demo buttons are
untouched (Features 5, 6, 9, 11).

## 2026-10-01 — Feature 2: dynamic DEM, hydrological and satellite data ingestion

**Built:** `backend/m1_terrain/ingest.py` (readiness check, DEM candidates with cache validation,
WorldCover with coverage check, DEM comparison + deterministic selection, HydroBASINS, discharge),
`hydrobasins.py` (local shapefile -> Earth Engine fallback, upstream walk reusing M7's pure helper),
`discharge.py` (GloFAS v4 via Open-Meteo Flood API), `config/m1_ingestion.yaml`. The onboarding
`terrain` stage now ingests then builds terrain (`onboarding.prepare_terrain`), logging each product
to the job; the worker records module error codes (`terrain_inputs_incomplete`). `download.py`:
WorldCover distinguishes absent tiles from network failures; download/pipeline CLIs honour
`SIH26_DATA_DIR`. `pipeline.py` copies the DEM selection into `terrain/provenance.json`. Frontend
wizard collects far/near-field cell sizes, near-field bounds and the breach location, and warns
which terrain fields are still empty. Decisions D1-D6 in `docs/decisions.md`; `src_075` added.

**Checks:** full backend `pytest -q` green; new `tests/m1_terrain/test_ingest.py`,
`tests/m0_api/test_ingestion_job.py`; frontend `tsc`, `check:shell`, `test:offline`,
`test:site-config` pass. All external services faked in tests (`SIH26_INGEST_NETWORK=off` in
`tests/m0_api/conftest.py`).

**Not yet done:** no live run against OpenTopography/WorldCover/Open-Meteo from this sandbox — run one
on the team machine with a real key. Gauge/CSV discharge upload would need a new API endpoint
(contract change) and is not part of this change.

## 2026-10-01 — Feature 3: dynamic breach modelling and hydrograph generation

**Built:** XZ9 peak discharge (`xz9.py`, F3-D1) and the XZ9 width label fix; status-based per-dam
placeholder warnings and `SIH26_DATA_DIR` defaults (`breach_params.py`, `hydrograph.py`);
`backend/m2_breach/reference.py` — the breach stage (`run_breach_stage`): parameters for every dam,
reference low/high hydrographs from M2's range bounds (weir when sourced, else triangular; blocked
with a reason otherwise), Feature 2 evidence in provenance, one job event per dam and case.
`onboarding.prepare_breach` and the worker use it. Frontend breach marker/tooltip use the first dam
of any site (`source.firstDam` / `firstBreachDam`) instead of `south_lhonak`. Decisions F3-D1..D6.

**Teesta (placeholder inputs):** south_lhonak Q_p range is now computable (4289-7123 m³/s) and its
reference low/high triangular hydrographs are written; teesta_iii (FD) keeps a blocked Q_p range
(Z20, F3-D2), and as a triggered cascade dam it gets no reference hydrograph (needs the routed
stage-1 inflow).

**Checks:** full backend `pytest -q` green; frontend `tsc`, `check:shell`, `test:offline`,
`test:site-config` pass. Updated (deliberately) the blocked-XZ9 assertions in
`test_equations.py`, `test_breach_params.py`, `test_hydrograph.py`.

## 2026-10-01 — Feature 4: dynamic scenario generation and what-if

**Built:** `scenario_design.py` reads `breach_params.json`, samples Q_p when the hydrograph is triangular,
samples volume only over `emulator_inputs`, hydrograph-validates every scenario, records full provenance,
fingerprints, archives history and never reuses IDs. `whatif.py` + `GET /sites/{id}/design` and
`POST /sites/{id}/whatif` (real M2 + hydrograph, optional save as `__n_<slug>`). `SiteConfig.emulator_inputs`.
Design-stage job event. Library screen shows the real design and runs/saves real what-ifs.

**Teesta (placeholder inputs):** 30 design + 5 held-out scenarios for south_lhonak, sampled B_ave, T_f, Q_p,
triangular hydrographs, none rejected. teesta_iii (FD) what-if is blocked (Z20) as expected.

**Checks:** full `pytest -q` green; frontend `tsc`, `check:shell`, `test:offline`, `test:site-config`,
new `test:scenarios`.

**For Feature 5:** `build_case` still needs `domains.far_field.inflow.base_flow` (null for Teesta).


## 2026-10-01 — Feature 5 phase 1: D-Flow FM preflight, data-dir fixes, real-kernel smoke test

**Built:** `scripts/dflowfm_preflight.py` (logic in `backend/m3_dflowfm/preflight.py`): read-only
PASS/WARNING/BLOCKED checks for platform, kernel (`--kernel`, `SIH26_DFLOWFM_KERNEL`, documented default),
`/usr/bin/time`, Python modules (hydrolib-core/meshkernel pins), free disk (`--min-free-gb`, no built-in
threshold), site config, terrain inputs, breach/reference hydrographs, scenario design (incl.
breach_params sha256) and base flow. A null base flow is BLOCKED; no value is supplied.
`backend/m3_dflowfm/smoke.py`: runs one case on the configured kernel through `launcher.launch_case` and
reports `solver_success`, `postprocess_success` and `wet_at_pois` separately, plus kernel provenance
(`/usr/bin/time` command line, `.dia` Program/Version written after launch) and downstream wet faces.

**Fixed:** `generator.build_case` defaulted to `<repo>/data` (import-time `DATA_DIR`), ignoring
`SIH26_DATA_DIR`; `campaign.run_dflowfm_campaign` built a missing design without its `data_dir`.

**Tests:** `test_preflight.py`, `test_data_dir_propagation.py` (fail on the old code),
`test_smoke_harness.py`, `test_real_solver.py` (real kernel; skips with the reason when
`SIH26_DFLOWFM_KERNEL` is unset). Full `pytest -q` green.

**Not done (by design):** no real run in this sandbox (no kernel); Teesta base flow still null; no
study-reach/outlet config; no multi-scenario campaign. Found, not fixed: `postprocess._solver_wall_seconds`
regex never matches GNU time's "(h:mm:ss or m:ss)" line, so `run_meta.wall_time_s` falls back to `.dia` stamps.

## 2026-10-01 — Feature 5: smoke-case stall diagnosed (mesh/terrain edge sills)

**Real-kernel evidence (team WSL run):** solver, provenance, forcing, source volume and mass balance all
fine; the source-connected wet region stopped ~1.7 km below the source; outlet dry.

**Cause (A+B, not solver, not post-processing):** the shared synthetic valley is a sharp V (zero-width
thalweg). With `BedLevType = 3` a flow link is bounded by its edge's higher node; the 30 m triangular
mesh put only 47 of 2,850 nodes within 15 m of the thalweg, so down-valley edges have one node up the
wall. Rebuilt net, frontier face 762 (bed 2162.554625 m, WSE 2166.278832 m): edge 762→782 nodes
2150.554 / 2167.968 m (sill 1.69 m above WSE, though face 782 is lower); 762→743 and 762→744 sills
2175.930 m. Best source→outlet path needs 7.7 m of ponding (`hydraulic_path.py`).

**Correction (synthetic smoke case only):** `synthetic_valley` takes an optional `floor_half_width_m`
(default 0 = unchanged); the smoke test uses a 240 m flat floor (`synth_m3_channel_terrain_dir`), whose
mesh needs ≤ 0.5 m ponding to reach the outlet. Smoke run extended to 2 h after t0 (outlet 11.4 km away).
The harness now reports `mesh_hydraulic_path` and `outlet` (faces touching the outlet polyline) and the
real test asserts the outlet is wet; the downstream assertion is unchanged. Production generator,
BedLevType and Teesta inputs unchanged. **Not yet re-run on the real kernel.**

**Open:** the generic generator's interior triangles are coarser than the configured spacing (median edge
40 m, max 189 m at 30 m), the likely reason real-site cases (Teesta 77k) also leave POIs dry. Teesta
base flow still null.

# PS-26161 compliance matrix (Feature 12)

Requirements are the explicit items of the problem statement as broken down in `docs/ideation.md`
("Build a software tool that:" + "Hidden Constraints"). Status values:

- **IMPLEMENTED**: works end to end and is tested on synthetic data; where real data exists it is used and labelled.
- **PARTIAL**: code path exists and runs, but the real-site result is limited or blocked by data/hardware.
- **BLOCKED**: code path exists; a real-world input (data, credentials, accepted run) is missing.
- **NOT IMPLEMENTED**: no code path.

Nothing here claims validation: model-to-model and literature comparisons are comparisons, and no
accepted observed flood extent exists for any site (`docs/real_input_checklist.json`).
Every evidence path below is checked to exist by `tests/test_ps_compliance.py`.

<!-- matrix:start -->
| # | Requirement | Evidence | Status | Notes |
|---|---|---|---|---|
| 1 | Simulate dam-break / natural-dam failure with SPH | `backend/m4_sph/generator.py`, `backend/m4_sph/launcher.py`, `tests/m4_sph/test_compare_mvp.py` | PARTIAL | Real DualSPHysics runs exist on the team machine; no accepted target-site run. Solver tests skip without `DSPH_BIN_DIR`. |
| 2 | Simulate with Delft3D (D-Flow FM) | `backend/m3_dflowfm/generator.py`, `backend/m3_dflowfm/launcher.py`, `backend/m0_api/real_query.py` | PARTIAL | Real Teesta MVP run on reconstructed forcing; fails POI smoke acceptance; production gate BLOCKED. |
| 3 | Compare SPH vs Delft3D | `backend/m4_sph/compare.py`, `tests/m0_api/test_compare_pair_api.py`, `frontend/app/sentriq/app.tsx` | BLOCKED | Pairing/comparison plumbing done; no accepted paired run, so the UI shows unavailable. Comparison is never labelled validation. |
| 4 | Predict inundated area | `backend/m5_emulator/emulator.py`, `backend/m5_emulator/fallback.py`, `backend/m0_api/real_query.py` | IMPLEMENTED | Emulator prediction / solver run / empirical fallback each labelled. No real Teesta-trained emulator yet. |
| 5 | Estimate loss / damage | `backend/m6_impact/loss.py`, `backend/m6_impact/impact.py` | IMPLEMENTED | JRC depth-damage curves; needs exposure layers; placeholders flagged. |
| 6 | GUI dashboard for input and output | `frontend/app/sentriq/app.tsx`, `frontend/src/data/dashboard.ts` | IMPLEMENTED | Canonical backend responses only; separate validation / comparison / simulation / emulator / monitoring status. |
| 7 | Output .shp / .kml | `backend/m6_impact/exports.py`, `tests/m0_api/test_feature12_provenance.py` | IMPLEMENTED | Also GeoJSON/PDF; every format carries provenance. |
| 8 | Near-real-time Google Earth Engine layer | `backend/m7_gee/fetch.py`, `tests/m7_gee/test_real_gee_smoke.py` | PARTIAL | Pipeline + cache + fallback tested with a fake EE; the real EE smoke test needs credentials and has not been run here. |
| 9 | Real Indian river + dam with open data | `sites/teesta.yaml`, `docs/real_input_checklist.json` | PARTIAL | Teesta demo with sourced open data; many inputs placeholder; production gate BLOCKED. |
| 10 | Any river/dam (new-site onboarding) | `backend/m0_api/onboarding.py`, `tests/m0_api/test_synthetic_onboarding_pipeline.py`, `config/registered_runs.yaml` | IMPLEMENTED | Config-driven; Teesta-only runtime behaviour moved to config/run metadata (Feature 12). |
| 11 | Uncertainty under missing breach parameters | `backend/m2_breach/breach_params.py`, `backend/m5_emulator/monte_carlo.py`, `backend/m5_emulator/confidence.py` | IMPLEMENTED | Dual-method ranges, unknown-breach Monte Carlo, S/C/U confidence; real emulator skill UNKNOWN. |
| 12 | Historical event validation | `backend/m0_api/validation_helpers.py`, `docs/real_input_checklist.json` | BLOCKED | No observed flood extent. Literature reconstruction is shown as a comparison only (`validation_available` stays false). |
<!-- matrix:end -->

## Genuine blockers (not fixable in code)

- No accepted observed flood extent for any event.
- No real-site emulator LOOCV report (no Teesta-trained emulator).
- No accepted paired SPH / D-Flow FM near-field run.
- Teesta production inputs (dam geometry, storage curve, base flow, POIs) are placeholders.
- Earth Engine credentials for the live smoke test.

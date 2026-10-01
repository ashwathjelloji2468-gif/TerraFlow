// Single seam between the UI and real data.
//
// Legacy UI adapters call the contract client in api.ts. Responses whose
// Estimate and raster/file forms cannot fit the old canvas models stay empty
// instead of being converted into guessed values. Components must not fetch.
//
// Two functions in this file (listScenarios, listSavedRuns) have no
// endpoint in the current contract. They are flagged below; ask before
// inventing routes for them.
import type {Grid, Params, Result} from '@/lib/model';
import type {Scenario} from '@/lib/sentriq';
import {api, useMocks, type FloodQueryRequest, type FloodQueryResponse, type ImpactResponse, type CompareResponse, type GeeLayers, type Timeline, type SiteSummary, type SiteDetail, type JobStatus, type ValidationResponse, type HistoricalValidationResponse, type Scene3DResponse} from './api';
import uiText from '../content/ui_text.json';
import * as offlineCache from '../offline/cache-store';
import {collectGlobalUrls, collectResourceUrls, type OfflineBundle} from '../offline/resource-list';
import {buildSiteConfig, validateWizardSite, type WizardSite} from './site-config';

export type {WizardSite, DamKind} from './site-config';
export {siteIdFromName, validateWizardSite, missingTerrainFields} from './site-config';

export type Awaiting = {status: 'awaiting'; reason: string};

export type {SiteSummary} from './api';
export type {SavedQuery} from '../offline/cache-store';
export type {OfflineBundle} from '../offline/resource-list';

/** Mock switch and the one registered direct solver scenario exposed for the MVP site. */
export function isMockMode(): boolean { return useMocks; }
export function directEventScenarioId(siteId: string): string | undefined {
  return !useMocks && siteId === 'teesta' ? 'teesta_2023_mvp' : undefined;
}

/** Contract §5.1 — GET /sites. */
export async function listSites(): Promise<SiteSummary[]> {
  return api.sites();
}

/** Contract §5.1 — GET /sites, GET /sites/{site_id}. */
export async function getSite(): Promise<SiteSummary | null> {
  const sites = await api.sites();
  return sites[0] ?? null;
}

/** Terrain for the active site (part of site detail / GET /scene3d/{query_id}). */
export async function getTerrain(siteId?: string): Promise<Awaiting & {grid: Grid | null}> {
  if (siteId) await api.site(siteId);
  else await api.sites();
  return {status: 'awaiting', reason: 'Site contract loaded; terrain grids are exposed through scene3d binary files, not the legacy Grid array required by this canvas.', grid: null};
}

/** Contract §5.4/§5 #10 — POST /flood/query, GET /flood/{query_id}. Covers
 * both the rapid-query slider and the Physics-run form. */
export async function queryFlood(request: FloodQueryRequest): Promise<FloodQueryResponse> {
  if (!request.site_id) throw new Error('A site_id is required to run a flood query.');
  let response = await api.floodQuery(request);
  if (response.status === 'failed') throw new Error('Flood query failed.');
  while (response.status === 'partial') {
    await new Promise(resolve => setTimeout(resolve, 500));
    response = await api.flood(response.query_id);
    if (response.status === 'failed') throw new Error('Flood query failed.');
  }
  return response;
}

/** Typed adapter from the canonical LayerRef to the existing image-overlay renderer.
 * Uses the response already returned by queryFlood; it does not fetch a second result
 * or infer raster values/georeferencing from the legacy Grid model. */
export type FloodRasterOverlay = {
  layerId: string; url: string; boundsLatLng: [[number, number], [number, number]];
  styleId: string; unit: string | null;
};
export function floodRasterOverlay(response: FloodQueryResponse | null, layerId = 'depth_p50'): FloodRasterOverlay | null {
  const refs = response?.layers.filter(layer => layer.available && layer.type === 'raster_png') ?? [];
  // The frozen contract example only contains p_inundation. Preserve that mock
  // fixture path while real responses must provide the requested layer.
  const ref = refs.find(layer => layer.layer_id === layerId) ?? (useMocks ? refs[0] : undefined);
  if (!ref || ref.bounds_latlng.length !== 2 || ref.bounds_latlng.some(point => point.length !== 2)) return null;
  return {layerId: ref.layer_id, url: api.fileUrl(ref.url), boundsLatLng: ref.bounds_latlng as [[number, number], [number, number]], styleId: ref.style_id, unit: ref.unit};
}

/** The existing playback control selects a contract Timeline snapshot URL.
 * Feed that file reference into the same raster image-overlay renderer used by
 * the static depth layer; no values or bounds are reconstructed in the UI. */
export function timelineRasterOverlay(timeline: Timeline | null, frameIndex: number): FloodRasterOverlay | null {
  const frames = timeline?.frames ?? [];
  if (!frames.length) return null;
  const frame = frames[Math.max(0, Math.min(frames.length - 1, frameIndex))];
  return {layerId: 'timeline_depth', url: api.fileUrl(frame.median_url),
    boundsLatLng: frame.bounds_latlng as [[number, number], [number, number]],
    styleId: 'depth_p50', unit: 'm'};
}

/** The prepared scenario library for the active site. No endpoint exists for
 * this in docs/handoff_contract.md §5 yet — ask before wiring this to a
 * real route. */
export async function listScenarios(): Promise<Awaiting & {scenarios: Scenario[]}> {
  return {status: 'awaiting', reason: 'No scenario library exists for this site yet.', scenarios: []};
}

/** Contract §5 #14 / §4.7 — GET /impact/{query_id}. */
export async function getImpact(queryId: string): Promise<ImpactResponse> {
  if (!queryId) throw new Error('A query_id is required to load impact results.');
  return api.impact(queryId);
}

/** Contract §5 #15 — GET /compare/{site_id}. */
export async function getCompare(siteId: string, scenarioId?: string): Promise<CompareResponse> {
  if (!siteId) throw new Error('A site_id is required to load model comparison.');
  return api.compare(siteId, scenarioId);
}
/** Contract §5.7 — GET /validation/{site_id}. */
export async function getValidation(siteId: string): Promise<ValidationResponse> {
  if (!siteId) throw new Error('A site_id is required to load validation status.');
  return api.validation(siteId);
}
/** Contract §5.7 — GET /validation/{site_id}?event={event_id}. */
export async function getHistoricalValidation(siteId: string, eventId: string): Promise<HistoricalValidationResponse> {
  if (!siteId) throw new Error('A site_id is required to load historical validation.');
  if (!eventId) throw new Error('An event_id is required to load historical validation.');
  return api.historicalValidation(siteId, eventId);
}

/** Contract §5 #19/#20 — GET /gee/{site_id}, POST /gee/{site_id}/refresh. */
export async function getObserved(siteId: string): Promise<GeeLayers> {
  if (!siteId) throw new Error('A site_id is required to load satellite layers.');
  return api.gee(siteId);
}
/** Resolve a contract file reference through the shared API client origin. */
export function fileUrl(path: string): string { return api.fileUrl(path); }
export async function refreshObserved(siteId: string): Promise<GeeLayers> {
  if (!siteId) throw new Error('A site_id is required to refresh satellite layers.');
  return api.refreshGee(siteId);
}
export async function getTimeline(queryId: string, intervalS = 300): Promise<Timeline> {
  if (!queryId) throw new Error('A query_id is required to load the flood timeline.');
  return api.timeline(queryId, intervalS);
}

/** Contract §5.9 — GET /scene3d/{query_id}. */
export async function getScene3d(queryId: string, verticalExaggeration = 1.5): Promise<Scene3DResponse> {
  if (!queryId) throw new Error('A query_id is required to load the 3D scene.');
  return api.scene3d(queryId, verticalExaggeration);
}

/** Fetches the scene's two headerless float32_le_row_major arrays (contracts/scene3d.md).
 * Kept here, not in the component, per this file's "components must not fetch" rule. */
export type Scene3DArrays = {terrain: Float32Array; flood: Float32Array};
export async function getScene3dArrays(scene: Scene3DResponse): Promise<Scene3DArrays> {
  const [terrainRes, floodRes] = await Promise.all([api.file(scene.terrain.url), api.file(scene.flood_surface.url)]);
  if (!terrainRes.ok || !floodRes.ok) throw new Error('Failed to load the 3D scene terrain/flood arrays.');
  const [terrainBuf, floodBuf] = await Promise.all([terrainRes.arrayBuffer(), floodRes.arrayBuffer()]);
  return {terrain: new Float32Array(terrainBuf), flood: new Float32Array(floodBuf)};
}

/** Contract §5.1 — GET /sites/{site_id}, for the real dam list (location/breach_location). */
export async function getSiteDetail(siteId: string): Promise<SiteDetail> {
  if (!siteId) throw new Error('A site_id is required to load site detail.');
  return api.site(siteId) as Promise<SiteDetail>;
}

/** A run's own run_meta.json, read through the generic asset passthrough (contract §1.8
 * file layout under /api/v1/files/) -- not a schema-validated response, just the real
 * artifact file M3 already wrote. Returns null (never invents) when it isn't there. */
export async function getRunMeta(siteId: string, runId: string): Promise<Record<string, unknown> | null> {
  if (!siteId || !runId) return null;
  const res = await api.file(`files/${siteId}/runs/${runId}/run_meta.json`);
  if (!res.ok) return null;
  return res.json();
}

/** Any other real per-site artifact JSON under data/<site_id>/... exposed through the same
 * generic passthrough (breach hydrograph forcing provenance, breach_params.json). `relPath`
 * is relative to the site's data dir, e.g. "breach/breach_params.json". 404 -> null. */
export async function getSiteArtifactJson(siteId: string, relPath: string): Promise<Record<string, unknown> | null> {
  if (!siteId || !relPath) return null;
  const res = await api.file(`files/${siteId}/${relPath}`);
  if (!res.ok) return null;
  return res.json();
}

/** Contract §6 — GET /styles (contracts/styles.json). Drives map legends and
 * colour classes; see STYLE_GUIDE.md §2.6 for the frontend palette that
 * should seed it. */
export async function getStyles(): Promise<Awaiting & {styles: unknown | null}> {
  return {status: 'awaiting', reason: 'Styles loaded from the contract; no display change is applied until map data is available.', styles: await api.styles()};
}

/** Contract §5 #18 — GET /export/{query_id}?format=. */
export async function exportUrl(format: 'shp' | 'kml' | 'geojson' | 'pdf', queryId: string): Promise<{url: string}> {
  if (!queryId) throw new Error('A query_id is required to export flood results.');
  const response = await api.export(queryId, format);
  return {url: URL.createObjectURL(await response.blob())};
}

/** Contract §5.2 — POST /sites (Add a Dam). Builds a real site config from the wizard, posts it,
 * and returns the persistent site id and onboarding job id. Throws with the server's own
 * message on 409/422 (duplicate id or name, invalid field) so the UI can show it. */
export async function createSite(input: WizardSite): Promise<{siteId: string; jobId: string}> {
  const problems = validateWizardSite(input);
  if (problems.length) throw new Error(problems.join(' '));
  const accepted = await api.createSite({site_config: buildSiteConfig(input), demo_mode: false});
  return {siteId: accepted.site_id, jobId: accepted.job_id};
}

/** Contract §5.3 — GET /jobs/{job_id}. */
export async function getJob(jobId: string): Promise<JobStatus> {
  if (!jobId) throw new Error('A job_id is required to load job status.');
  return api.job(jobId);
}

/** Saved runs / sites. No endpoint exists for this in the contract yet (the
 * prototype used Cloudflare D1/R2 + ChatGPT auth, which rule 11 disallows).
 * Ask before wiring this to a real route. */
export async function listSavedRuns(): Promise<Awaiting & {records: unknown[]}> {
  return {status: 'awaiting', reason: 'Saved-run storage is not connected yet.', records: []};
}
export async function saveRun(_kind: 'run' | 'site', _name: string, _data: unknown): Promise<Awaiting & {record: unknown | null}> {
  return {status: 'awaiting', reason: 'Saved-run storage is not connected yet.', record: null};
}

/** Offline caching (src/offline/) — registers the app-shell service worker
 * once at startup; components never touch Cache Storage or the worker
 * directly, only through this seam. */
export function initOffline(): void { offlineCache.registerOfflineWorker(); }
export function isOfflineCacheSupported(): boolean { return offlineCache.isCacheSupported(); }
export function listSavedOfflineQueries() { return offlineCache.listSavedQueries(); }
export function latestSavedOfflineQuery() { return offlineCache.latestSavedQuery(); }
export function deleteSavedOfflineQuery(queryId: string): void { offlineCache.deleteSavedQuery(queryId); }

/** Estimates the download size of a "save for offline" of this bundle without saving anything. */
export async function estimateOfflineSaveSize(bundle: OfflineBundle) {
  return offlineCache.estimateSaveSize([...collectGlobalUrls(), ...collectResourceUrls(bundle)]);
}

/** Fetches and stores every resource behind an already-loaded query so the
 * dashboard can reopen it later with no connection. */
export async function saveQueryForOffline(bundle: OfflineBundle, siteName: string, onProgress?: (p: {done: number; total: number}) => void) {
  const urls = [...collectGlobalUrls(), ...collectResourceUrls(bundle)];
  return offlineCache.saveForOffline(bundle.siteId, bundle.floodQuery.query_id, siteName, urls, onProgress);
}

/** Reopens a previously saved query straight from the cache — never a fresh
 * POST /flood/query, which would mint a new, uncached query_id. */
export async function loadSavedOfflineQuery(queryId: string): Promise<FloodQueryResponse> {
  return api.flood(queryId);
}

/** Feature 3: the most-upstream dam (contract §5.1 `order` 1; site configs list dams upstream
 * first), used for the breach marker/tooltip -- never a hard-coded site or dam id. */
export function firstDam<T extends {order: number}>(dams: T[] | undefined): T | undefined {
  return dams?.length ? [...dams].sort((a, b) => a.order - b.order)[0] : undefined;
}

/** `breach_params.json` keeps `dams` in config order (upstream first, contract §4.2). */
export function firstBreachDam<T>(dams: T[] | undefined): T | undefined {
  return dams?.[0];
}


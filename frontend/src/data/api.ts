/// <reference types="vite/client" />

import breachParams from '../../../contracts/examples/breach_params.example.json';
import compare from '../../../contracts/examples/compare.example.json';
import errorExample from '../../../contracts/examples/error.example.json';
import extent from '../../../contracts/examples/extent_geojson.example.json';
import floodRequest from '../../../contracts/examples/flood_query_request.example.json';
import floodResponse from '../../../contracts/examples/flood_query_response.example.json';
import geeLayers from '../../../contracts/examples/gee_layers.example.json';
import geojson from '../../../contracts/examples/geojson_feature_collection.example.json';
import health from '../../../contracts/examples/health.example.json';
import historicalValidation from '../../../contracts/examples/historical_validation.example.json';
import impact from '../../../contracts/examples/impact.example.json';
import jobAccepted from '../../../contracts/examples/job_accepted.example.json';
import jobStatus from '../../../contracts/examples/job_status.example.json';
import scenarioDesign from '../../../contracts/examples/scenario_design.example.json';
import scene3d from '../../../contracts/examples/scene3d.example.json';
import siteAccepted from '../../../contracts/examples/site_create_accepted.example.json';
import siteDetail from '../../../contracts/examples/site_detail.example.json';
import siteList from '../../../contracts/examples/site_list.example.json';
import siteRequest from '../../../contracts/examples/site_create_request.example.json';
import siteSummary from '../../../contracts/examples/site_summary.example.json';
import styles from '../../../contracts/styles.json';
import timeline from '../../../contracts/examples/timeline.example.json';
import validation from '../../../contracts/examples/validation.example.json';

export type FloodQueryRequest = {
  site_id: string;
  scenario_id?: string;
  model: 'delft3d' | 'sph';
  mode: 'scenario' | 'unknown_breach';
  inputs: Record<string, {type: 'exact'; value: number} | {type: 'slider'; position: number}>;
  options?: {n_samples?: number; seed?: number | null};
};
export type Estimate = {
  value: number | null; low: number | null; high: number | null;
  unit: string | null; interval: 'P10-P90' | 'method_range' | 'zone_range' | 'none';
  kind: 'predicted' | 'observed' | 'input'; confidence?: 'HIGH' | 'MODERATE' | 'LOW' | null;
  basis?: string; source?: string;
};
export type FloodQueryResponse = {
  contract_version: string; query_id: string; site_id: string;
  status: 'partial' | 'complete' | 'failed'; method: 'gp_emulator' | 'empirical_fallback' | 'delft3d_direct' | 'sph_direct';
  mode: 'scenario' | 'unknown_breach';
  resolved_inputs: Record<string, Estimate>;
  summary: {
    inundated_area_m2: Estimate; max_depth_m: Estimate; max_velocity_ms: Estimate;
    peak_discharge_m3s: Estimate;
    first_arrival: {poi_id: string; name: string; arrival_s: Estimate};
  };
  confidence: Record<'overall' | 'extent' | 'depth' | 'arrival' | 'velocity', {
    level: 'HIGH' | 'MODERATE' | 'LOW'; components: Record<string, string>; reason_key: string | null;
  }>;
  layers: Array<{layer_id: string; label_key?: string; type: string; url: string; bounds_latlng: number[][]; style_id: string; unit: string | null; available: boolean}>;
  vectors: {extent_url: string};
  flags: {outside_trained_range: boolean; demo_mode: boolean; library_outdated: boolean; has_placeholders: boolean};
  placeholder_fields: string[];
  caveats: Array<{id: string; severity: 'info' | 'warning' | 'critical'; text_key: string}>;
  provenance: Record<string, unknown>;
  timing_ms: {median_phase: number; full_phase: number};
};
export type ImpactResponse = {
  contract_version: string; query_id: string; site_id: string;
  population_persons: Estimate;
  assets: {
    buildings: {high: number; possible: number}; roads_m: {high: number; possible: number};
    bridges: {high: number; possible: number}; hospitals: {high: number; possible: number};
    schools: {high: number; possible: number}; cropland_m2: {high: number; possible: number};
    hydropower: Array<{name: string; zone: 'high' | 'possible'; depth_m: Estimate}>;
  };
  loss_inr: Estimate & {by_asset_class?: Record<string, Estimate>; assumptions?: string[]};
  warning_table: Array<{poi_id: string; name: string; kind: string; chainage_m: number; zone: 'high' | 'possible'; p_inundation: number; arrival_s: Estimate; depth_m: Estimate; velocity_ms: Estimate}>;
  not_affected_poi_count: number; data_coverage_notes: string[];
  has_placeholders: boolean; placeholder_fields: string[];
  caveats: Array<{id: string; severity: 'info' | 'warning' | 'critical'; text_key: string}>;
  provenance: Record<string, unknown>;
};
export type CompareResponse = {
  site_id: string; scenario_id: string;
  sph_vs_delft3d: {available: boolean; domain: string; time_window_s: number; metrics: {iou?: number; f1_0_3?: number; depth_rmse_wet_m?: number; velocity_mae_ms?: number}; probes: Array<{poi_id: string; arrival_delft3d_s: number; arrival_sph_s: number; diff_s: number}>; layers: FloodQueryResponse['layers']; run_ids: string[]};
  emulator_vs_physics: {available: boolean; held_out_run_id: string | null; metrics: {iou?: number; depth_rmse_wet_m?: number; arrival_mae_s?: number}; layers: FloodQueryResponse['layers']};
  gp_vs_linear: {iou_median_gp?: number; iou_median_linear?: number; arrival_mae_s_gp?: number; arrival_mae_s_linear?: number}; when_to_use_key: string;
  caveats: Array<{id: string; severity: 'info' | 'warning' | 'critical'; text_key: string}>;
};
export type GeeLayers = {
  site_id: string; source: 'live' | 'cache' | 'screenshot_fallback'; fetched_at: string;
  lake_area_series: Array<{date: string; area_m2: number; method: 's2_water_index' | 's1_threshold'; cloud_pct: number | null}>;
  lake_latest: {type: 'FeatureCollection'; features: Array<{type: 'Feature'; geometry: {type: string; coordinates: unknown}; properties: Record<string, unknown>}>};
  rainfall: Array<{date: string; precip_mm: number; dataset: 'chirps' | 'gpm_imerg'}>;
  imagery: Array<{event_id: string; phase: 'pre' | 'post'; date: string; url: string; bounds_latlng: number[][]}>;
  observed_extents: Array<{event_id: string; url: string; method: 'manual_digitized' | 'change_detection'}>;
  recheck: {outdated: boolean; change_pct: number | null; threshold_pct: number};
};
export type Timeline = {
  query_id: string; interval_s: number; t_end_s: number;
  frames: Array<{t_s: number; median_url: string; high_url: string; possible_url: string; bounds_latlng: number[][]}>;
  hydrographs: Array<{dam_id: string; t_offset_s: number; points: Array<{t_s: number; q_m3s: number}>}>;
  arrival_profile: Array<{chainage_m: number; arrival_p10_s: number|null; arrival_p50_s: number; arrival_p90_s: number|null}>;
  pois_on_profile: Array<{poi_id: string; name: string; chainage_m: number}>;
  caveats: Array<{id: string; severity: 'info' | 'warning' | 'critical'; text_key: string}>;
  provenance: Record<string, unknown>;
};
export type ValidationResponse = {
  contract_version: string; site_id: string; model: 'delft3d' | 'sph'; n_runs: number;
  per_run: Array<Record<string, unknown>>; summary: Record<string, unknown>;
  baseline_linear: Record<string, unknown>; grade_thresholds_ref: string; events: string[];
  synthetic_loocv?: {
    world: string; note: string; model?: string; n_runs?: number;
    summary?: Record<string, unknown>; baseline_linear?: Record<string, unknown>;
    baseline_nearest?: Record<string, unknown>; acceptance?: Record<string, unknown>;
    grade_thresholds_ref?: string;
  } | null;
};
export type HistoricalValidationResponse = {
  contract_version: string; site_id: string; event_id: string;
  observed: {available?: boolean; extent_url?: string; note?: string} & Record<string, unknown>;
  predicted: Record<string, unknown>;
  metrics: Record<string, unknown>;
  comparison_domain: string;
  caveats: Array<{id: string; severity: 'info' | 'warning' | 'critical'; text_key: string}>;
  provenance: Record<string, unknown>;
  literature_comparison?: {
    available: boolean; poi: string;
    simulated: Record<string, unknown> | null;
    literature: Array<{source_id: string; citation: string; quantity: string; value: unknown; note: string}>;
    caveats: string[];
  };
};
export type SiteSummary = {
  site_id: string; name: string;
  status: 'onboarding' | 'demo_mode' | 'ready' | 'outdated' | 'failed';
  status_reason_key?: string | null; bbox_lonlat?: [number, number, number, number];
  has_placeholders?: boolean;
};
export type JobStatus = {
  job_id: string; kind: 'onboarding' | 'campaign' | 'recheck' | 'rerun';
  site_id: string; stage: string; stage_label_key: string;
  progress: {current: number | null; total: number | null; unit: string | null};
  eta_s?: number | null; demo_mode?: boolean; started_at: string | null;
  updated_at: string; log_tail?: string[]; error: Record<string, unknown> | null;
};
/** A dam entry's key_specs is an open dict of SourcedValues by contract (§5.1) --
 * this doesn't add a schema field, just types what real sites already put there. */
export type SourcedValue = {value: unknown; unit: string | null; source: string | null; status: string};
export type SiteDetail = SiteSummary & {
  dams: Array<{dam_id: string; name: string; kind: string | null; order: number; key_specs: Record<string, SourcedValue>}>;
};
export type SiteCreateRequest = {site_config: Record<string, unknown>; demo_mode?: boolean};
export type SiteCreateAccepted = {job_id: string; site_id: string};
/** Contract §5.9 / contracts/scene3d.md — GET /scene3d/{query_id}. */
export type Scene3DResponse = {
  contract_version: string; query_id: string;
  frame: {
    crs_epsg: number | null; origin_x_utm_m: number; origin_y_utm_m: number;
    vertical_exaggeration: number; vertical_exaggeration_applies_to: string;
    units: string; axis_order: string;
  };
  terrain: {
    url: string; encoding: string; width: number; height: number;
    cell_size_x_m: number; cell_size_y_m: number;
    origin_x_utm_m: number; origin_y_utm_m: number;
    origin_local_x_m: number; origin_local_y_m: number;
    transform: number[]; crs_epsg: number | null;
    min_elev_m: number | null; max_elev_m: number | null;
    nodata: number; byte_length: number;
  };
  flood_surface: {
    url: string; encoding: string; nodata: number; basis: string;
    width: number; height: number; byte_length: number;
  };
  comparison: {
    nearfield_bounds_local: number[][];
    delft3d_surface_url: string | null; delft3d_surface_basis: string | null;
    sph_surfaces: Array<{t_s: number; url: string}>;
  };
  payload_bytes: number; max_payload_mb: number;
};

const baseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1').replace(/\/$/, '');
export const useMocks = import.meta.env.VITE_USE_MOCKS === 'true';

const fixtures: Record<string, unknown> = {
  health, styles, sites: siteList, site: siteDetail, siteSummary, siteRequest,
  siteAccepted, jobAccepted, jobStatus, floodRequest, floodResponse, impact,
  compare, validation, historicalValidation, geeLayers, scene3d, timeline,
  extent, geojson, breachParams, scenarioDesign, error: errorExample,
};

/** Contract §2.7 error message, either bare or wrapped by FastAPI's HTTPException as `{detail: ...}`. */
function errorMessage(payload: unknown): string | undefined {
  type Envelope = {error?: {message?: string}};
  const body = payload as (Envelope & {detail?: Envelope}) | undefined;
  return body?.error?.message ?? body?.detail?.error?.message;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly payload?: unknown) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, options: RequestInit = {}, fixture?: keyof typeof fixtures): Promise<T> {
  if (useMocks && fixture) return structuredClone(fixtures[fixture]) as T;
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {'Content-Type': 'application/json', ...options.headers},
  });
  const payload: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    const message = errorMessage(payload);
    throw new ApiError(message || `API request failed (${response.status})`, response.status, payload);
  }
  return payload as T;
}

const json = (body: unknown): RequestInit => ({method: 'POST', body: JSON.stringify(body)});

export const api = {
  health: () => request('/health', {}, 'health'),
  styles: () => request('/styles', {}, 'styles'),
  sites: () => request<SiteSummary[]>('/sites', {}, 'sites'),
  site: (siteId: string) => request<SiteDetail>(`/sites/${encodeURIComponent(siteId)}`, {}, 'site'),
  createSite: (body: SiteCreateRequest) => request<SiteCreateAccepted>('/sites', json(useMocks ? siteRequest : body), 'siteAccepted'),
  job: (jobId: string) => request<JobStatus>(`/jobs/${encodeURIComponent(jobId)}`, {}, 'jobStatus'),
  recheck: (siteId: string, body: unknown) => request(`/sites/${encodeURIComponent(siteId)}/recheck`, {method: 'PUT', body: JSON.stringify(body)}, 'siteSummary'),
  rerun: (siteId: string) => request(`/sites/${encodeURIComponent(siteId)}/rerun`, {method: 'POST'}, 'jobAccepted'),
  floodQuery: (body: FloodQueryRequest) => request<FloodQueryResponse>('/flood/query', json(useMocks ? floodRequest : body), 'floodResponse'),
  flood: (queryId: string) => request<FloodQueryResponse>(`/flood/${encodeURIComponent(queryId)}`, {}, 'floodResponse'),
  timeline: (queryId: string, intervalS = 300) => request<Timeline>(`/flood/${encodeURIComponent(queryId)}/timeline?interval_s=${intervalS}`, {}, 'timeline'),
  extent: (queryId: string) => request(`/flood/${encodeURIComponent(queryId)}/extent.geojson`, {}, 'extent'),
  impact: (queryId: string) => request<ImpactResponse>(`/impact/${encodeURIComponent(queryId)}`, {}, 'impact'),
  compare: (siteId: string, scenarioId?: string) => request<CompareResponse>(`/compare/${encodeURIComponent(siteId)}${scenarioId ? `?scenario_id=${encodeURIComponent(scenarioId)}` : ''}`, {}, 'compare'),
  validation: (siteId: string) => request<ValidationResponse>(`/validation/${encodeURIComponent(siteId)}`, {}, 'validation'),
  historicalValidation: (siteId: string, eventId: string) => request<HistoricalValidationResponse>(`/validation/${encodeURIComponent(siteId)}?event=${encodeURIComponent(eventId)}`, {}, 'historicalValidation'),
  export: async (queryId: string, format: 'shp' | 'kml' | 'geojson' | 'pdf') => {
    const response = await fetch(`${baseUrl}/export/${encodeURIComponent(queryId)}?format=${format}`);
    if (!response.ok) {
      const payload = await response.json().catch(() => undefined);
      const message = errorMessage(payload);
      throw new ApiError(message || `API request failed (${response.status})`, response.status, payload);
    }
    return response;
  },
  gee: (siteId: string) => request<GeeLayers>(`/gee/${encodeURIComponent(siteId)}`, {}, 'geeLayers'),
  refreshGee: (siteId: string) => request<GeeLayers>(`/gee/${encodeURIComponent(siteId)}/refresh`, {method: 'POST'}, 'geeLayers'),
  scene3d: (queryId: string, verticalExaggeration = 1.5) => request<Scene3DResponse>(`/scene3d/${encodeURIComponent(queryId)}?vertical_exaggeration=${verticalExaggeration}`, {}, 'scene3d'),
  file: (path: string) => {
    const relativePath = path.replace(/^\/api\/v1\/?/, '').replace(/^\/+/, '');
    return fetch(`${baseUrl}/${relativePath}`);
  },
  /** Resolve contract file references against the configured API origin (for image overlays). */
  fileUrl: (path: string) => {
    if (/^https?:\/\//i.test(path)) return path;
    const relativePath = path.replace(/^\/api\/v1\/?/, '').replace(/^\/+/, '');
    return `${baseUrl}/${relativePath}`;
  },
  // Contract example fixture access for contract data with no GET route.
  examples: {scenarioDesign, siteRequest, breachParams, geojson, floodRequest},
};

export type Api = typeof api;

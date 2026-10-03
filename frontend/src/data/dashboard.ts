// Feature 11: pure helpers that turn canonical backend responses (FloodQuery, Impact, Compare,
// GeeLayers, Validation) into dashboard / compare / export display state. Nothing here computes a
// flood number: every value is read from a response, and a missing value is `null` -> "Unavailable",
// never 0. Re-exported through src/data/source.ts (the frontend data seam).
import type {SiteReadiness, CompareResponse, Estimate, FloodQueryResponse, GeeLayers, HistoricalValidationResponse, ImpactResponse, ValidationResponse} from './api';

export const UNAVAILABLE = 'Unavailable';

export type MethodInfo = {label: string; kind: 'emulator_prediction' | 'solver_run' | 'fallback' | 'none'; detail: string};
export function methodInfo(q: FloodQueryResponse | null): MethodInfo {
  if (!q) return {label: 'No query yet', kind: 'none', detail: 'Run a query to see results.'};
  switch (q.method) {
    case 'gp_emulator': return {label: 'Emulator prediction (M5)', kind: 'emulator_prediction', detail: 'Gaussian-process emulator prediction, not a solver run.'};
    case 'delft3d_direct': return {label: 'D-Flow FM solver run', kind: 'solver_run', detail: directDetail(q, 'D-Flow FM')};
    case 'sph_direct': return {label: 'DualSPHysics solver run', kind: 'solver_run', detail: directDetail(q, 'DualSPHysics')};
    case 'empirical_fallback': return {label: 'Empirical fallback', kind: 'fallback', detail: 'Empirical breach + HAND routing; no trained emulator.'};
    default: return {label: String(q.method), kind: 'none', detail: ''};
  }
}

/** Feature 12: a direct solver run's caveat comes from its own provenance (`input_forcing_note`,
 * e.g. the Teesta MVP's reconstructed forcing), never from a hardcoded site string. */
export function directDetail(q: FloodQueryResponse, solver: string): string {
  const note = (q.provenance as {input_forcing_note?: string | null})?.input_forcing_note;
  return note || `Real ${solver} solver output · not validated against observed data.`;
}

/** A finite estimate value, else null (never a substituted zero). */
export function estimateValue(e: Estimate | null | undefined, scale = 1): number | null {
  const v = e?.value;
  return typeof v === 'number' && Number.isFinite(v) ? v * scale : null;
}

export type DashboardMetric = {key: string; label: string; value: number | null; low: number | null; high: number | null; unit: string; source: string};
const range = (e: Estimate | null | undefined, scale = 1) => ({
  low: typeof e?.low === 'number' && Number.isFinite(e.low) ? e.low * scale : null,
  high: typeof e?.high === 'number' && Number.isFinite(e.high) ? e.high * scale : null,
});

/** Dashboard metric cards, strictly from FloodQueryResponse + ImpactResponse. */
export function dashboardMetrics(q: FloodQueryResponse | null, impact: ImpactResponse | null): DashboardMetric[] {
  const s = q?.summary;
  const flood = q ? `FloodQuery ${q.query_id}` : UNAVAILABLE;
  const imp = impact ? `Impact ${impact.query_id}` : UNAVAILABLE;
  const earliest = impact?.warning_table?.map(r => estimateValue(r.arrival_s)).filter((v): v is number => v !== null);
  const firstArrival = earliest && earliest.length ? Math.min(...earliest) : estimateValue(s?.first_arrival?.arrival_s);
  return [
    {key: 'area', label: 'Inundated area', value: estimateValue(s?.inundated_area_m2, 1e-6), ...range(s?.inundated_area_m2, 1e-6), unit: 'km²', source: flood},
    {key: 'depth', label: 'Maximum depth', value: estimateValue(s?.max_depth_m), ...range(s?.max_depth_m), unit: 'm', source: flood},
    {key: 'velocity', label: 'Maximum velocity', value: estimateValue(s?.max_velocity_ms), ...range(s?.max_velocity_ms), unit: 'm/s', source: flood},
    {key: 'arrival', label: 'Earliest arrival', value: firstArrival === null || firstArrival === undefined ? null : firstArrival / 60, low: null, high: null, unit: 'min', source: earliest && earliest.length ? imp : flood},
    {key: 'population', label: 'Affected population', value: estimateValue(impact?.population_persons), ...range(impact?.population_persons), unit: 'people', source: imp},
  ];
}

export type ZoneCounts = {available: boolean; high: number | null; possible: number | null; pois_high: number; pois_possible: number};
export function zoneCounts(impact: ImpactResponse | null): ZoneCounts {
  if (!impact) return {available: false, high: null, possible: null, pois_high: 0, pois_possible: 0};
  return {available: true, high: impact.assets?.buildings?.high ?? null, possible: impact.assets?.buildings?.possible ?? null,
    pois_high: impact.warning_table.filter(r => r.zone === 'high').length,
    pois_possible: impact.warning_table.filter(r => r.zone === 'possible').length};
}

export type GeeStatus = {label: 'LIVE' | 'CACHE' | 'FALLBACK' | 'NONE' | 'UNAVAILABLE'; latestObservation: string | null; lake: boolean; rainfall: boolean; partial: boolean; fetchedAt: string | null};
export function geeStatus(gee: GeeLayers | null): GeeStatus {
  if (!gee) return {label: 'UNAVAILABLE', latestObservation: null, lake: false, rainfall: false, partial: false, fetchedAt: null};
  const lakeRows = gee.lake_area_series.filter(r => r.area_m2 !== null);
  const latest = String(gee.lake_latest.features[0]?.properties?.date ?? lakeRows.at(-1)?.date ?? '') || null;
  const label = gee.data_available === false ? (gee.source === 'screenshot_fallback' ? 'FALLBACK' : 'NONE')
    : gee.source === 'live' ? 'LIVE' : gee.source === 'screenshot_fallback' ? 'FALLBACK' : 'CACHE';
  return {label, latestObservation: latest, lake: lakeRows.length > 0, rainfall: gee.rainfall.length > 0,
    partial: !!gee.partial, fetchedAt: gee.fetched_at || null};
}

export type ValidationStatus = {emulator: boolean; historical: boolean; observed: boolean; literatureComparison: boolean; note: string};
export function validationStatus(v: ValidationResponse | null, h: HistoricalValidationResponse | null): ValidationStatus {
  const emulator = !!v && v.validation_available !== false && !v.synthetic_demo && v.n_runs > 0 && v.per_run.length > 0;
  const observed = !!h && h.observed?.available === true;
  // Feature 12: historical VALIDATION needs accepted observed data; literature reconstructions are a
  // comparison and can never make this true, whatever the backend flag says.
  const historical = observed && (h!.provenance as {validation_available?: boolean})?.validation_available === true;
  const literatureComparison = !!h && ((h.provenance as {comparison_available?: boolean})?.comparison_available === true
    || (h as unknown as {literature_comparison?: {available?: boolean}}).literature_comparison?.available === true);
  return {emulator, historical, observed, literatureComparison,
    note: 'Model-to-model comparison (SPH vs D-Flow FM, emulator vs solver, GP vs linear) is not field validation.'};
}

export type CompareOption = {scenario_id: string; kind: string; label: string};
/** Scenario selector options from the persisted design rows (GET /sites/{id}/design). */
export function compareScenarioOptions(rows: Array<{scenario_id: string; kind: string}> | null | undefined): CompareOption[] {
  const seen = new Set<string>();
  return (rows ?? []).filter(r => r.scenario_id && !seen.has(r.scenario_id) && seen.add(r.scenario_id))
    .map(r => ({scenario_id: r.scenario_id, kind: r.kind, label: `${r.scenario_id} · ${r.kind}`}));
}

export type CompareSection = {available: boolean; reason: string | null; runIds: string[]; layers: CompareResponse['emulator_vs_physics']['layers']};
export function compareSections(c: CompareResponse | null): {sph: CompareSection; emulator: CompareSection; gpLinear: boolean} {
  const none: CompareSection = {available: false, reason: null, runIds: [], layers: []};
  if (!c) return {sph: none, emulator: none, gpLinear: false};
  return {
    sph: {available: c.sph_vs_delft3d.available && (!c.sph_vs_delft3d.status || c.sph_vs_delft3d.status.comparison_status === 'SUCCEEDED'),
      reason: c.sph_vs_delft3d.unavailable_reason ?? c.sph_vs_delft3d.status?.comparison_reason ?? null,
      runIds: c.sph_vs_delft3d.run_ids, layers: c.sph_vs_delft3d.layers.filter(l => l.available)},
    emulator: {available: c.emulator_vs_physics.available, reason: c.emulator_vs_physics.unavailable_reason ?? null,
      runIds: c.emulator_vs_physics.held_out_run_id ? [c.emulator_vs_physics.held_out_run_id] : [],
      layers: c.emulator_vs_physics.layers.filter(l => l.available)},
    gpLinear: Object.keys(c.gp_vs_linear).length > 0,
  };
}

export type ExportFormat = 'shp' | 'kml' | 'geojson' | 'pdf';
/** What each export of THIS query will actually contain, from the responses, not generic claims. */
export function exportContents(q: FloodQueryResponse | null, impact: ImpactResponse | null): Record<ExportFormat, string[]> {
  const out: Record<ExportFormat, string[]> = {shp: [], kml: [], geojson: [], pdf: []};
  if (!q) return out;
  const hz = impact?.hazard_layers ?? {};
  const has = (k: string) => !!hz[k]?.path;
  const vec = ['flood extent / zone polygons'];
  if (has('depth_classes')) vec.push('depth-class polygons');
  if (has('isochrones')) vec.push('arrival isochrones');
  if (impact?.warning_table.length) vec.push(`${impact.warning_table.length} warning POIs`);
  out.shp = [...vec]; out.kml = [...vec]; out.geojson = [...vec];
  out.pdf = [`${methodInfo(q).label} report`, 'summary estimates', impact ? 'impact + warning table' : 'impact unavailable', 'caveats + provenance'];
  return out;
}

/** Safe filename from a Content-Disposition header, else a deterministic fallback. */
export function exportFilename(disposition: string | null, siteId: string, queryId: string, format: ExportFormat): string {
  const m = disposition?.match(/filename="?([^";]+)"?/i);
  const raw = m?.[1] ?? `${siteId}_${queryId}.${format === 'shp' ? 'zip' : format}`;
  return raw.replace(/[^A-Za-z0-9._-]/g, '_');
}

export type StatusChip = {key: 'validation' | 'comparison' | 'simulation' | 'emulator' | 'monitoring'; label: string; state: string; ok: boolean};
/** Feature 12: five separate, never-conflated status chips (validation is not comparison, a
 * simulation is not an emulator prediction, cached monitoring is not live). */
export function statusChips(q: FloodQueryResponse | null, c: CompareResponse | null, v: ValidationResponse | null,
  h: HistoricalValidationResponse | null, gee: GeeLayers | null): StatusChip[] {
  const val = validationStatus(v, h), cmp = compareSections(c), g = geeStatus(gee), m = methodInfo(q);
  const validated = val.emulator || val.historical;
  const comparisonOk = cmp.sph.available || cmp.emulator.available || cmp.gpLinear || val.literatureComparison;
  return [
    {key: 'validation', label: 'Validation', state: validated ? 'Available' : 'Validation unavailable', ok: validated},
    {key: 'comparison', label: 'Comparison', state: comparisonOk ? 'Comparison available (not validation)' : 'Comparison unavailable', ok: comparisonOk},
    {key: 'simulation', label: 'Simulation', state: m.kind === 'solver_run' ? `${m.label} available` : 'No solver run for this query', ok: m.kind === 'solver_run'},
    {key: 'emulator', label: 'Emulator', state: m.kind === 'emulator_prediction' ? 'Emulator prediction available' : 'No emulator prediction', ok: m.kind === 'emulator_prediction'},
    {key: 'monitoring', label: 'Monitoring', state: g.label === 'UNAVAILABLE' || g.label === 'NONE' ? 'Monitoring unavailable' : `Monitoring ${g.label}`, ok: g.label === 'LIVE' || g.label === 'CACHE'},
  ];
}

export type ReadinessSummary = {verdict: 'READY' | 'BLOCKED' | 'UNAVAILABLE'; blockingCount: number;
  counts: Record<string, number>; runCounts: Record<string, number>; topBlocking: string[]; pilotRuns: string[]; acceptedRuns: string[]};
/** Feature 13: compact production-readiness summary. No readiness response -> UNAVAILABLE, never READY. */
export function readinessSummary(r: SiteReadiness | null): ReadinessSummary {
  if (!r) return {verdict: 'UNAVAILABLE', blockingCount: 0, counts: {}, runCounts: {}, topBlocking: [], pilotRuns: [], acceptedRuns: []};
  return {verdict: r.production_verdict === 'READY' && r.blocking.length === 0 ? 'READY' : 'BLOCKED',
    blockingCount: r.blocking.length, counts: r.counts, runCounts: r.run_counts, topBlocking: r.blocking.slice(0, 5),
    pilotRuns: r.runs.filter(x => x.status === 'PILOT').map(x => x.run_id),
    acceptedRuns: r.runs.filter(x => x.status === 'ACCEPTED').map(x => x.run_id)};
}

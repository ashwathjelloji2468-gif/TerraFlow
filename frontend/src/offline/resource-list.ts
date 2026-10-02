// Builds the exact absolute URLs "save for offline" should snapshot, given the
// data already loaded on screen for one flood query. Kept separate from
// cache-store.ts so the knowledge of which contract fields carry file/URL
// references lives in one place, not scattered across components.
import {api, type FloodQueryResponse, type ImpactResponse, type CompareResponse, type GeeLayers, type Timeline, type ValidationResponse, type Scene3DResponse} from '../data/api';

export type OfflineExportFormat = 'shp' | 'kml' | 'geojson' | 'pdf';

export type OfflineBundle = {
  siteId: string;
  floodQuery: FloodQueryResponse;
  impact: ImpactResponse | null;
  compare: CompareResponse | null;
  timeline: Timeline | null;
  validation: ValidationResponse | null;
  gee: GeeLayers | null;
  scene3d: Scene3DResponse | null;
  exportFormats?: OfflineExportFormat[];
  /** Extra `files/{siteId}/...`-relative paths the caller already knows it
   * needs (e.g. the 3D breach marker's run_meta.json / breach_params.json /
   * forcing provenance JSON) but that don't come from a typed bundle field. */
  extraFilePaths?: (string | null | undefined)[];
};

/** Resources every saved query needs regardless of site (drives the site
 * selector and map legends on a cold offline boot). */
export function collectGlobalUrls(): string[] {
  return [api.fileUrl('styles'), api.fileUrl('sites')];
}

export function collectResourceUrls(bundle: OfflineBundle): string[] {
  const urls = new Set<string>();
  const add = (path?: string | null) => { if (path) urls.add(api.fileUrl(path)); };
  const queryId = bundle.floodQuery.query_id;

  add(`sites/${bundle.siteId}`);
  add(`flood/${queryId}`);
  add(`flood/${queryId}/extent.geojson`);
  add(`impact/${queryId}`);
  add(`compare/${bundle.siteId}`);
  if (bundle.compare?.scenario_id) add(`compare/${bundle.siteId}?scenario_id=${encodeURIComponent(bundle.compare.scenario_id)}`);
  for (const layer of [...(bundle.compare?.sph_vs_delft3d.layers ?? []), ...(bundle.compare?.emulator_vs_physics.layers ?? [])]) if (layer.available) add(layer.url);
  add(`validation/${bundle.siteId}`);
  for (const eventId of bundle.validation?.events ?? []) add(`validation/${bundle.siteId}?event=${encodeURIComponent(eventId)}`);
  add(`gee/${bundle.siteId}`);

  for (const layer of bundle.floodQuery.layers) if (layer.available) add(layer.url);
  add(bundle.floodQuery.vectors?.extent_url);

  if (bundle.timeline) {
    add(`flood/${queryId}/timeline?interval_s=${bundle.timeline.interval_s}`);
    for (const frame of bundle.timeline.frames) { add(frame.median_url); add(frame.high_url); add(frame.possible_url); }
  }
  if (bundle.scene3d) {
    add(`scene3d/${queryId}?vertical_exaggeration=${bundle.scene3d.frame.vertical_exaggeration}`);
    add(bundle.scene3d.terrain.url);
    add(bundle.scene3d.flood_surface.url);
  }
  if (bundle.gee) for (const image of bundle.gee.imagery) add(image.url);
  for (const format of bundle.exportFormats ?? ['shp', 'kml', 'geojson', 'pdf']) add(`export/${queryId}?format=${format}`);
  for (const path of bundle.extraFilePaths ?? []) if (path) add(`files/${bundle.siteId}/${path}`);

  return Array.from(urls);
}

/** Feature 12: the provenance recorded with an offline save, read only from the bundle's own
 * backend responses (nothing inferred). Keeps cache/fallback/placeholder state explicit. */
export function offlineProvenance(bundle: OfflineBundle): Record<string, unknown> {
  const prov = (bundle.floodQuery.provenance ?? {}) as Record<string, any>;
  return {
    site_id: bundle.siteId, query_id: bundle.floodQuery.query_id, method: bundle.floodQuery.method,
    scenario_id: prov.parameters?.scenario_id ?? prov.scenario_id ?? null, run_ids: prov.run_ids ?? [],
    output_classification: prov.output_classification ?? null,
    result_generated_at: prov.generated_at ?? prov.created_at ?? null,
    code_version: prov.code_version ?? null, contract_version: bundle.floodQuery.contract_version,
    has_placeholders: !!bundle.floodQuery.flags?.has_placeholders, demo_mode: !!bundle.floodQuery.flags?.demo_mode,
    caveats: bundle.floodQuery.caveats.map(c => c.id),
    impact_available: !!bundle.impact,
    compare_scenario_id: bundle.compare?.scenario_id ?? null,
    gee_source: bundle.gee ? (bundle.gee.data_available === false ? 'none' : bundle.gee.source) : null,
    validation_available: bundle.validation ? bundle.validation.validation_available === true : null,
  };
}

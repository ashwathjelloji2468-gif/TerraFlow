// Feature 11 frontend checks: dashboard / compare / export / GEE display state comes only from
// canonical backend responses (synthetic fixtures here, stubbed fetch, no backend).
// Run with `npm run test:dashboard`.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createServer} from 'vite';

const server = await createServer({
  configFile: new URL('../vite.config.ts', import.meta.url).pathname,
  server: {middlewareMode: true}, appType: 'custom',
});
let failures = 0;
async function test(name, fn) {
  try { await fn(); console.log(`ok - ${name}`); } catch (e) { failures++; console.error(`FAIL - ${name}`); console.error(e); }
}
const ex = n => JSON.parse(readFileSync(new URL(`../../contracts/examples/${n}.example.json`, import.meta.url)));
const flood = ex('flood_query_response'), impact = ex('impact'), design = ex('scenario_design'), gee = ex('gee_layers');
const calls = [];
globalThis.fetch = async (url) => {
  const u = String(url); calls.push(u);
  if (u.includes('/design')) return new Response(JSON.stringify(design), {status: 200});
  if (u.includes('/compare/')) return new Response(JSON.stringify({site_id: 'synth', scenario_id: new URL(u, 'http://x').searchParams.get('scenario_id') ?? '',
    sph_vs_delft3d: {available: false, domain: 'nearfield', time_window_s: 0, metrics: {}, probes: [], layers: [], run_ids: [], unavailable_reason: 'No paired SPH/D-Flow FM run.'},
    emulator_vs_physics: {available: false, held_out_run_id: null, metrics: {}, layers: [], unavailable_reason: 'No LOOCV sidecar.'},
    gp_vs_linear: {}, when_to_use_key: 'comparison_unavailable', caveats: []}), {status: 200});
  if (u.includes('/export/q_missing')) return new Response(JSON.stringify({detail: {error: {code: 'query_not_found', message: "No registered query artifacts for 'q_missing'.", details: {}}}}), {status: 404});
  if (u.includes('/export/')) {
    const fmt = new URL(u, 'http://x').searchParams.get('format');
    const name = fmt === 'pdf' ? 'synth_q1_report.pdf' : `synth_q1.${fmt === 'shp' ? 'zip' : fmt}`;
    return new Response('x', {status: 200, headers: {'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${name}"`}});
  }
  return new Response('{}', {status: 500});
};
globalThis.URL.createObjectURL = () => 'blob:test';
try {
  const src = await server.ssrLoadModule('/src/data/source.ts');
  const rl = await server.ssrLoadModule('/src/offline/resource-list.ts');

  await test('dashboard metrics are the FloodQuery/Impact values, not local synthetic sums', () => {
    const m = Object.fromEntries(src.dashboardMetrics(flood, impact).map(x => [x.key, x]));
    assert.equal(m.depth.value, flood.summary.max_depth_m.value);
    assert.equal(m.area.value, flood.summary.inundated_area_m2.value * 1e-6);
    assert.equal(m.population.value, impact.population_persons.value);
    assert.ok(m.depth.source.includes(flood.query_id));
  });
  await test('no query -> every metric unavailable (null), never 0', () => {
    for (const x of src.dashboardMetrics(null, null)) { assert.equal(x.value, null); assert.equal(x.source, src.UNAVAILABLE); }
    const bad = structuredClone(flood); bad.summary.max_depth_m.value = null;
    assert.equal(src.dashboardMetrics(bad, null).find(x => x.key === 'depth').value, null);
    assert.equal(src.zoneCounts(null).available, false);
  });
  await test('M5 is labelled emulator prediction, D-Flow FM as solver run', () => {
    assert.match(src.methodInfo({...flood, method: 'gp_emulator'}).label, /Emulator prediction/);
    assert.equal(src.methodInfo({...flood, method: 'gp_emulator'}).kind, 'emulator_prediction');
    const note = 'MVP reconstructed South Lhonak forcing · real D-Flow FM output · not scientifically validated.';
    const d = src.methodInfo({...flood, method: 'delft3d_direct', provenance: {...flood.provenance, input_forcing_note: note}});
    assert.match(d.label, /D-Flow FM/); assert.equal(d.detail, note);  // Teesta MVP caveat preserved, from provenance
    const other = src.methodInfo({...flood, method: 'delft3d_direct', provenance: {}});
    assert.doesNotMatch(other.detail, /Lhonak|MVP/); assert.match(other.detail, /not validated/);  // Feature 12: no Teesta text elsewhere
  });
  await test('design rows populate compare options; selected scenario reaches /compare', async () => {
    const d = await src.getScenarioDesign('synth');
    const opts = src.compareScenarioOptions(d.rows);
    assert.equal(opts.length, new Set(d.rows.map(r => r.scenario_id)).size);
    const c = await src.getCompare('synth', opts[0].scenario_id);
    assert.ok(calls.at(-1).endsWith(`/compare/synth?scenario_id=${encodeURIComponent(opts[0].scenario_id)}`));
    assert.equal(c.scenario_id, opts[0].scenario_id);
    const s = src.compareSections(c);
    assert.equal(s.sph.available, false); assert.equal(s.emulator.available, false);
    assert.equal(s.sph.reason, 'No paired SPH/D-Flow FM run.'); assert.deepEqual(s.sph.layers, []);
  });
  await test('available emulator-vs-physics layers are passed through with their URLs', () => {
    const c = ex('compare');
    c.sph_vs_delft3d.status = {solver_status: 'BLOCKED', postprocess_status: 'BLOCKED', comparison_status: 'BLOCKED', validation_status: 'BLOCKED'};
    c.emulator_vs_physics.layers = [{layer_id: 'depth_diff', type: 'raster_png', url: '/api/v1/files/synth/emulator/delft3d/validation/compare/s1__delft3d__depth_diff.png', bounds_latlng: [[1, 2], [3, 4]], style_id: 'depth_diff', unit: 'm', available: true}];
    const s = src.compareSections(c);
    assert.equal(s.sph.available, false, 'a non-SUCCEEDED pair status is never shown as available');
    assert.equal(s.emulator.layers[0].url, c.emulator_vs_physics.layers[0].url);
  });
  await test('each export format calls the real endpoint and uses the backend filename', async () => {
    for (const fmt of ['shp', 'kml', 'geojson', 'pdf']) {
      const r = await src.exportUrl(fmt, 'q1', 'synth');
      assert.ok(calls.at(-1).endsWith(`/export/q1?format=${fmt}`));
      assert.equal(r.filename, fmt === 'pdf' ? 'synth_q1_report.pdf' : `synth_q1.${fmt === 'shp' ? 'zip' : fmt}`);
    }
    assert.equal(src.exportFilename('attachment; filename="../../etc passwd"', 's', 'q', 'kml'), '.._.._etc_passwd');
    assert.equal(src.exportFilename(null, 's', 'q', 'shp'), 's_q.zip');
  });
  await test('backend 404 on export is surfaced, nothing generated in the browser', async () => {
    await assert.rejects(src.exportUrl('geojson', 'q_missing', 'synth'), e => e.status === 404 && /q_missing/.test(e.message));
    await assert.rejects(src.exportUrl('geojson', ''), /query_id is required/);
    assert.deepEqual(src.exportContents(null, null), {shp: [], kml: [], geojson: [], pdf: []});
  });
  await test('export contents reflect the actual impact hazard layers', () => {
    const i = structuredClone(impact);
    i.hazard_layers = {depth_classes: {path: 'depth_classes.geojson'}, isochrones: {path: null, status: 'unavailable'}};
    const c = src.exportContents(flood, i);
    assert.ok(c.geojson.includes('depth-class polygons')); assert.ok(!c.geojson.includes('arrival isochrones'));
    assert.ok(src.exportContents(flood, null).pdf.includes('impact unavailable'));
  });
  await test('GEE status: LIVE / CACHE / FALLBACK / NONE and partial', () => {
    assert.equal(src.geeStatus({...gee, source: 'live', partial: true}).label, 'LIVE');
    assert.equal(src.geeStatus({...gee, source: 'live', partial: true}).partial, true);
    assert.equal(src.geeStatus({...gee, source: 'cache'}).label, 'CACHE');
    assert.equal(src.geeStatus({...gee, source: 'screenshot_fallback', data_available: false}).label, 'FALLBACK');
    assert.equal(src.geeStatus({...gee, data_available: false, lake_area_series: [], rainfall: []}).label, 'NONE');
    assert.equal(src.geeStatus(null).label, 'UNAVAILABLE');
  });
  await test('validation status never claims validation from an empty or example-less report', () => {
    const empty = {contract_version: '0.3.0', site_id: 's', model: 'delft3d', n_runs: 0, per_run: [], summary: {}, baseline_linear: {}, grade_thresholds_ref: '', events: [], validation_available: false};
    const v = src.validationStatus(empty, null);
    assert.equal(v.emulator, false); assert.equal(v.historical, false); assert.match(v.note, /not field validation/);
  });
  await test('F12: literature-only historical report is comparison, never validation', () => {
    const h = {contract_version: '0.3.0', site_id: 's', event_id: 'e', observed: {available: false}, predicted: {}, metrics: {}, comparison_domain: 'x', caveats: [],
      provenance: {validation_available: true, comparison_available: true}, literature_comparison: {available: true}};
    const v = src.validationStatus(null, h);
    assert.equal(v.historical, false, 'no observed data -> never validation, even if a flag says so');
    assert.equal(v.literatureComparison, true);
  });
  await test('F12: five status chips stay separate', () => {
    const chips = Object.fromEntries(src.statusChips({...flood, method: 'gp_emulator'}, null, null, null, {...gee, source: 'cache'}).map(c => [c.key, c]));
    assert.deepEqual(Object.keys(chips), ['validation', 'comparison', 'simulation', 'emulator', 'monitoring']);
    assert.equal(chips.validation.state, 'Validation unavailable');
    assert.equal(chips.emulator.state, 'Emulator prediction available'); assert.equal(chips.simulation.ok, false);
    assert.equal(chips.monitoring.state, 'Monitoring CACHE');
    const direct = Object.fromEntries(src.statusChips({...flood, method: 'delft3d_direct'}, null, null, null, null).map(c => [c.key, c]));
    assert.equal(direct.simulation.ok, true); assert.equal(direct.emulator.ok, false); assert.equal(direct.monitoring.state, 'Monitoring unavailable');
  });
  await test('F12: site runtime behaviour comes from config, not hardcoded IDs', () => {
    assert.equal(src.demoSiteId(), 'teesta');
    assert.equal(src.siteRuntime('demo_valley').direct_event_scenario_id, undefined);
    assert.equal(src.directEventScenarioId('demo_valley'), undefined);
  });
  await test('F12: offline save records provenance from the bundle', () => {
    const fq = {...flood, provenance: {...flood.provenance, code_version: 'abc1234', generated_at: '2026-10-02T00:00:00Z', run_ids: ['r1']}};
    const meta = rl.offlineProvenance({siteId: 'synth', floodQuery: fq, impact: null, compare: null, timeline: null, validation: null, gee: {...gee, data_available: false}, scene3d: null});
    assert.equal(meta.code_version, 'abc1234'); assert.equal(meta.method, fq.method); assert.deepEqual(meta.run_ids, ['r1']);
    assert.equal(meta.gee_source, 'none'); assert.equal(meta.impact_available, false); assert.equal(meta.validation_available, null);
    assert.equal(meta.has_placeholders, !!fq.flags.has_placeholders);
    assert.deepEqual(JSON.parse(JSON.stringify(meta)), meta);
  });
  await test('offline bundle with Feature 11 compare stays serializable and collects layer URLs', () => {
    const compare = ex('compare'); compare.scenario_id = 's 1';
    const bundle = {siteId: 'synth', floodQuery: flood, impact, compare, timeline: null, validation: null, gee: null, scene3d: null};
    const roundTrip = JSON.parse(JSON.stringify(bundle));
    assert.deepEqual(roundTrip.compare, compare);
    const urls = rl.collectResourceUrls(bundle);
    assert.ok(urls.some(u => u.endsWith('/compare/synth?scenario_id=s%201')));
    for (const l of compare.sph_vs_delft3d.layers.filter(x => x.available)) assert.ok(urls.some(u => u.endsWith(l.url.replace(/^\/api\/v1/, ''))));
  });
} finally {
  await server.close();
}
if (failures) { console.error(`${failures} test(s) failed`); process.exit(1); }
console.log('\nAll dashboard tests passed.');

// Feature 16 P7: a new (synthetic, non-Teesta) site must not present itself as Teesta, readiness is shown
// in the production gate's own vocabulary, and rendering readiness makes exactly one read-only request.
// Synthetic fixtures + stubbed fetch; no backend, no network. Run with `npm run test:site-neutral`.
import assert from 'node:assert/strict';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, extname} from 'node:path';
import {createServer} from 'vite';

const ROOT = new URL('..', import.meta.url).pathname;
const server = await createServer({configFile: join(ROOT, 'vite.config.ts'), server: {middlewareMode: true}, appType: 'custom'});
let failures = 0;
async function test(name, fn) {
  try { await fn(); console.log(`ok - ${name}`); } catch (e) { failures++; console.error(`FAIL - ${name}`); console.error(e); }
}
const SYNTH = 'synth_river_alpha';
const TEESTA = /teesta|lhonak|chungthang/i;
const uiText = JSON.parse(readFileSync(join(ROOT, 'src/content/ui_text.json'), 'utf8'));
// ui_text keys that are ONLY rendered for Teesta's own data (a caveat id emitted by the Teesta MVP run, and the
// ISRO reference panel gated by site_runtime.json reference_panel). Everything else must be site-neutral.
const TEESTA_ONLY_KEYS = new Set(['floodQuery.caveats.mvp_reconstructed_forcing', 'monitoring.referenceTitle']);
function flat(obj, prefix = '') {
  return Object.entries(obj).flatMap(([k, v]) => (v && typeof v === 'object' ? flat(v, `${prefix}${k}.`) : [[`${prefix}${k}`, String(v)]]));
}
const readiness = {
  site_id: SYNTH, model: 'delft3d', evaluated_at: '2026-10-03T00:00:00Z', production_verdict: 'BLOCKED', config_loaded: true,
  inputs: [
    {key: 'crs.utm_epsg', category: 'site_config', status: 'READY', blocking: true, source: 'synthetic', detail: ''},
    {key: 'dams[0].location', category: 'site_config', status: 'PLACEHOLDER', blocking: true, source: '', detail: 'value is a placeholder, not a production input'},
    {key: 'domains.far_field.inflow.base_flow', category: 'site_config', status: 'BLOCKED', blocking: true, source: '', detail: 'value is null'},
    {key: 'terrain_artifacts', category: 'artifact', status: 'BLOCKED', blocking: true, source: null, detail: 'missing: dem.tif'},
    {key: 'm3_frozen_pilot_reproduction', category: 'shared_gate', status: 'CONTROLLED_PASS', blocking: false, source: null, detail: 'CONTROLLED PASS'},
  ],
  blocking: ['dams[0].location', 'domains.far_field.inflow.base_flow', 'terrain_artifacts'],
  counts: {READY: 1, PLACEHOLDER: 1, BLOCKED: 2, CONTROLLED_PASS: 1}, runs: [], run_counts: {}, caveats: [],
};

try {
  const src = await server.ssrLoadModule('/src/data/source.ts');

  await test('a synthetic site gets its own neutral labels, never the demo site\'s', () => {
    const l = src.siteLabels(SYNTH, 'Synthetic River Alpha');
    assert.deepEqual(l, {site: 'Synthetic River Alpha', event: 'registered event', short: 'SYNTHETIC RIVER ALPHA'});
    assert.doesNotMatch(JSON.stringify(src.siteLabels(SYNTH)), TEESTA);
    assert.deepEqual(src.siteRuntime(SYNTH), {});
  });

  await test('every templated ui_text string is Teesta-free for a synthetic site', () => {
    const labels = src.siteLabels(SYNTH, 'Synthetic River Alpha');
    const leaks = flat(uiText).filter(([k]) => !TEESTA_ONLY_KEYS.has(k))
      .map(([k, v]) => [k, src.fillText(v, labels)]).filter(([, v]) => TEESTA.test(v));
    assert.deepEqual(leaks, []);
  });

  await test('Teesta keeps its own labels from site_runtime.json (data, not UI code)', () => {
    assert.equal(src.demoSiteId(), 'teesta');
    const d = src.demoLabels();
    assert.equal(src.fillText(uiText.onboarding.demoOpen, d), 'Open Teesta III Demo');
    assert.equal(src.fillText(uiText.landing.demoSite, d), 'Teesta III · South Lhonak 2023');
    assert.equal(src.fillText(uiText.landing.demoIndex, d), '01 / TEESTA DEMO');
  });

  await test('fillText leaves unknown placeholders alone', () => {
    assert.equal(src.fillText('{site} / {nope}', {site: 'A'}), 'A / {nope}');
  });

  await test('readiness counts use the production-gate vocabulary only', () => {
    assert.deepEqual([...src.READINESS_STATUSES], ['READY', 'PLACEHOLDER', 'BLOCKED', 'CONTROLLED_PASS']);
    assert.deepEqual(src.readinessStatusCounts(readiness), {READY: 1, PLACEHOLDER: 1, BLOCKED: 2, CONTROLLED_PASS: 1});
    const s = src.readinessSummary(readiness);
    assert.equal(s.verdict, 'BLOCKED');
    assert.deepEqual(s.topBlocking, ['dams[0].location', 'domains.far_field.inflow.base_flow', 'terrain_artifacts']);
    assert.equal(src.readinessStatusCounts(null), null);
    assert.equal(src.readinessSummary(null).verdict, 'UNAVAILABLE'); // missing response is never READY
  });

  await test('candidate evidence and GloFAS are described as pending / modelled, never observed', () => {
    const note = uiText.readiness.onboardingNote;
    assert.match(note, /modelled, not gauged/);
    assert.match(note, /never applied automatically/);
    assert.match(note, /not production data/);
    assert.doesNotMatch(note, /observed|gauged discharge|approved/i);
  });

  await test('readiness is fetched once per settled onboarding job, never polled', () => {
    for (const st of ['queued', 'terrain', 'breach', 'design', null, undefined]) assert.equal(src.onboardingSettled(st), false, String(st));
    for (const st of ['simulating', 'training', 'validating', 'ready', 'failed']) assert.equal(src.onboardingSettled(st), true, st);
    const app = readFileSync(join(ROOT, 'app/sentriq/app.tsx'), 'utf8');
    const m = app.match(/source\.getReadiness\(onboardSiteId\)[^\n]*?\},\[([^\]]*)\]\);/);
    assert.ok(m, 'onboarding readiness effect not found');
    assert.equal(m[1], 'onboardSiteId,onboardSettled,offline'); // no siteJob object -> no per-poll refetch
    assert.doesNotMatch(app.slice(app.indexOf('onboardReadiness'), app.indexOf('onboardReadiness') + 600), /setInterval|setTimeout/);
  });

  await test('rendering readiness makes exactly one read-only GET, no acquisition endpoint', async () => {
    const calls = [];
    globalThis.fetch = async (url, init) => { calls.push([String(url), init?.method ?? 'GET']); return new Response(JSON.stringify(readiness), {status: 200}); };
    const r = await src.getReadiness(SYNTH);
    assert.equal(r.site_id, SYNTH);
    assert.equal(calls.length, 1);
    assert.match(calls[0][0], /\/sites\/synth_river_alpha\/readiness$/);
    assert.equal(calls[0][1], 'GET');
    assert.doesNotMatch(calls[0][0], /recheck|rerun|refresh|gee|ingest|download/i);
  });

  await test('no hard-coded Teesta string literal in UI code', () => {
    const files = [];
    const walk = d => { for (const n of readdirSync(d)) { const f = join(d, n); if (n === 'node_modules') continue;
      if (statSync(f).isDirectory()) walk(f); else if (['.ts', '.tsx'].includes(extname(n))) files.push(f); } };
    for (const d of ['app', 'lib', 'src', 'components', 'hooks']) walk(join(ROOT, d));
    const literal = /(['"`])(?:(?!\1)[^\n])*(teesta|lhonak|chungthang)(?:(?!\1)[^\n])*\1/i;
    const allowed = /ISRO_SOUTH_LHONAK_REFERENCE|isro_south_lhonak|deccanherald\.com|south-lhonak-lake/; // Teesta-only reference panel data
    const hits = [];
    for (const f of files) readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, '');
      if (literal.test(code) && !allowed.test(code)) hits.push(`${f.slice(ROOT.length)}:${i + 1}`);
    });
    assert.deepEqual(hits, []);
  });
} finally {
  await server.close();
}
if (failures) { console.error(`${failures} test(s) failed`); process.exit(1); }
console.log('\nAll site-neutral tests passed.');

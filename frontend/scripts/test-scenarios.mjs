// Feature 4 frontend checks: the real scenario listing and what-if request built from the UI form,
// against a stubbed fetch (no backend). Run with `npm run test:scenarios`.
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
const design = JSON.parse(readFileSync(new URL('../../contracts/examples/scenario_design.example.json', import.meta.url)));
const whatif = JSON.parse(readFileSync(new URL('../../contracts/examples/whatif_response.example.json', import.meta.url)));
const calls = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push({url: String(url), init});
  if (String(url).endsWith('/sites/nodesign/design')) return new Response(JSON.stringify({detail: {error: {code: 'design_not_found', message: 'none', details: {}}}}), {status: 404});
  if (String(url).endsWith('/design')) return new Response(JSON.stringify(design), {status: 200});
  if (String(url).endsWith('/whatif')) return new Response(JSON.stringify(whatif), {status: 200});
  return new Response('{}', {status: 500});
};
try {
  const src = await server.ssrLoadModule('/src/data/source.ts');
  await test('listing maps the real design (scenarios + extra) into rows', async () => {
    const out = await src.getScenarioDesign('teesta');
    assert.ok(calls.at(-1).url.endsWith('/sites/teesta/design'));
    assert.equal(out.rows.length, design.scenarios.length + design.extra.length);
    assert.equal(out.rows[0].scenario_id, design.scenarios[0].scenario_id);
    assert.equal(out.rows[0].breach_width_m, design.scenarios[0].params.breach_width_m);
  });
  await test('no design yet is null, not an invented list', async () => {
    assert.equal(await src.getScenarioDesign('nodesign'), null);
  });
  await test('what-if sends only the fields the user set', async () => {
    assert.deepEqual(src.whatIfRequest({case: 'low', breachWidthM: 60, waterVolumeM3: null, failureTimeS: NaN}),
      {case: 'low', inputs: {}, scenario: {breach_width_m: 60}});
    assert.deepEqual(src.whatIfRequest({case: 'high', waterVolumeM3: 4e7, peakDischargeM3s: 5000, saveAs: '  my_case '}),
      {case: 'high', inputs: {water_volume_m3: 4e7}, scenario: {peak_discharge_m3s: 5000}, save_as: 'my_case'});
  });
  await test('runWhatIf POSTs the request to the site endpoint', async () => {
    const r = await src.runWhatIf('teesta', {case: 'high', breachWidthM: 150});
    const call = calls.at(-1);
    assert.ok(call.url.endsWith('/sites/teesta/whatif'));
    assert.equal(call.init.method, 'POST');
    assert.deepEqual(JSON.parse(call.init.body), {case: 'high', inputs: {}, scenario: {breach_width_m: 150}});
    assert.equal(r.status, whatif.status);
  });
} finally {
  await server.close();
}
if (failures) { console.error(`${failures} failing`); process.exit(1); }
console.log('all scenario tests passed');

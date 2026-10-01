// Unit tests for src/data/site-config.ts (Feature 1 onboarding config builder), same
// ssrLoadModule convention as test-resource-list.mjs. Run with `npm run test:site-config`.
// SITE_CONFIG_OUT=<path> also writes one built config there, so the backend's SiteConfig
// model and JSON schema can be checked against exactly what the wizard sends.
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {createServer} from 'vite';

const server = await createServer({
  configFile: new URL('../vite.config.ts', import.meta.url).pathname,
  server: {middlewareMode: true},
  appType: 'custom',
});

let failures = 0;
function test(name, fn) {
  try { fn(); console.log(`ok - ${name}`); } catch (e) { failures++; console.error(`FAIL - ${name}`); console.error(e); }
}

try {
  const m = await server.ssrLoadModule('/src/data/site-config.ts');
  const wizard = {
    name: 'Kosi Barrage study reach', citation: '', damName: 'Kosi Barrage', damKind: 'embankment_dam',
    damLat: 26.5, damLon: 87.0, bbox: [86.9, 26.4, 87.2, 26.8],
    farResolutionM: 90, nearResolutionM: 30, nearBbox: [86.95, 26.45, 87.05, 26.55], breachLat: 26.49, breachLon: 87.0,
    breach: {waterVolumeM3: 5e7, waterHeightM: 12, breachHeightM: null, damHeightM: null,
             embankmentWidthM: null, damType: 'HD', failureMode: null, erodibility: null},
  };

  test('site id is a contract slug derived from the name', () => {
    assert.equal(m.siteIdFromName('Kosi Barrage study reach'), 'kosi_barrage_study_reach');
    assert.equal(m.siteIdFromName('  Teesta-III / Chungthang '), 'teesta_iii_chungthang');
    assert.match(m.siteIdFromName('9 Mile Lake'), /^[a-z][a-z0-9_]{2,31}$/);
    assert.match(m.siteIdFromName('x'), /^[a-z][a-z0-9_]{2,31}$/);
    assert.ok(m.siteIdFromName('a'.repeat(80)).length <= 32);
  });

  test('UTM EPSG from lon/lat', () => {
    assert.equal(m.utmEpsg(88.6, 27.9), 32645);
    assert.equal(m.utmEpsg(79.7, 30.4), 32644);
    assert.equal(m.utmEpsg(-70, -33), 32719);
  });

  test('wizard validation is explicit', () => {
    assert.deepEqual(m.validateWizardSite(wizard), []);
    assert.ok(m.validateWizardSite({...wizard, name: ' '}).some(e => /site name/.test(e)));
    assert.ok(m.validateWizardSite({...wizard, bbox: [87.2, 26.4, 86.9, 26.8]}).some(e => /west < east/.test(e)));
    assert.ok(m.validateWizardSite({...wizard, damLon: 90}).some(e => /inside the study-area/.test(e)));
  });

  test('built config: entered values are placeholders with a source, blanks are null placeholders', () => {
    const cfg = m.buildSiteConfig(wizard);
    assert.equal(cfg.site.id, 'kosi_barrage_study_reach');
    assert.deepEqual(cfg.domains.far_field.bbox.value, [86.9, 26.4, 87.2, 26.8]);
    const bi = cfg.dams[0].breach_inputs;
    assert.deepEqual(bi.water_volume_above_invert, {value: 5e7, unit: 'm^3', source: m.OPERATOR_SOURCE, status: 'placeholder'});
    assert.deepEqual(bi.breach_height, {value: null, unit: 'm', source: '', status: 'placeholder'});
    assert.equal(bi.dam_type.value, 'HD');
    assert.equal(cfg.domains.far_field.inflow.from, cfg.dams[0].id);
    const all = JSON.stringify(cfg);
    assert.ok(!all.includes('"status":"sourced"'), 'the wizard never marks a value as sourced');
  });

  test('Feature 2 terrain fields are sent and checked', () => {
    const cfg = m.buildSiteConfig(wizard);
    assert.equal(cfg.domains.far_field.grid_resolution.value, 90);
    assert.equal(cfg.domains.near_field.grid_resolution.value, 30);
    assert.deepEqual(cfg.domains.near_field.bbox.value, [86.95, 26.45, 87.05, 26.55]);
    assert.deepEqual(cfg.dams[0].breach_location.value, [87.0, 26.49]);
    assert.deepEqual(m.missingTerrainFields(wizard), []);
    assert.deepEqual(m.missingTerrainFields({...wizard, nearBbox: null, breachLat: null}), ['near-field bounds', 'breach location']);
    assert.ok(m.validateWizardSite({...wizard, nearResolutionM: 40}).some(e => /divide/.test(e)));
    assert.ok(m.validateWizardSite({...wizard, nearBbox: [86.0, 26.45, 87.05, 26.55]}).some(e => /inside the study-area/.test(e)));
    assert.ok(m.validateWizardSite({...wizard, breachLon: 95}).some(e => /breach location/.test(e)));
  });

  test('a citation replaces the operator-entry source', () => {
    const cfg = m.buildSiteConfig({...wizard, citation: 'CWC dam register 2024'});
    assert.equal(cfg.dams[0].location.source, 'CWC dam register 2024');
  });

  if (process.env.SITE_CONFIG_OUT) writeFileSync(process.env.SITE_CONFIG_OUT, JSON.stringify(m.buildSiteConfig(wizard), null, 2));
} finally {
  await server.close();
}

if (failures) { console.error(`${failures} failing`); process.exit(1); }
console.log('all site-config tests passed');

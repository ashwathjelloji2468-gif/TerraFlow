// Feature 1 -- builds a contract site config (docs/handoff_contract.md §3,
// contracts/schemas/site_config.schema.json) from what the onboarding wizard collected.
//
// Nothing is invented: every value the operator typed is sent as `status: placeholder`
// with the source "operator entry (TerraFlow onboarding wizard), unverified" unless they
// gave a citation, and every value they left blank is sent as `value: null, status:
// placeholder`. The backend then propagates `has_placeholders: true` (CLAUDE.md rule 3).

export const OPERATOR_SOURCE = 'operator entry (TerraFlow onboarding wizard), unverified';

export type DamKind = 'moraine_dammed_lake' | 'embankment_dam' | 'concrete_dam' | 'landslide_dam';
export type WizardSite = {
  name: string;
  citation?: string;
  damName: string;
  damKind: DamKind;
  damLat: number;
  damLon: number;
  bbox: [number, number, number, number] | null; // [min_lon, min_lat, max_lon, max_lat]
  breach: {
    waterVolumeM3: number | null;
    waterHeightM: number | null;
    breachHeightM: number | null;
    damHeightM: number | null;
    embankmentWidthM: number | null;
    damType: 'HD' | 'CD' | 'FD' | 'ZD' | null;
    failureMode: 'O' | 'P' | null;
    erodibility: 'H' | 'M' | 'L' | null;
  };
};

/** A contract site id (§1.7 `^[a-z][a-z0-9_]{2,31}$`) derived from the site name. */
export function siteIdFromName(name: string): string {
  let id = name.normalize('NFKD').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  if (!/^[a-z]/.test(id)) id = `site_${id}`;
  id = id.slice(0, 32).replace(/_+$/, '');
  return id.length >= 3 ? id : `${id}_site`.slice(0, 32);
}

/** UTM zone EPSG for a lon/lat (WGS 84 north 326nn, south 327nn). */
export function utmEpsg(lon: number, lat: number): number {
  const zone = Math.min(60, Math.max(1, Math.floor((lon + 180) / 6) + 1));
  return (lat >= 0 ? 32600 : 32700) + zone;
}

export function validateWizardSite(w: WizardSite): string[] {
  const errors: string[] = [];
  if (!w.name.trim()) errors.push('Enter a site name.');
  if (!w.damName.trim()) errors.push('Enter the dam or lake name.');
  if (!(w.damLat >= -85 && w.damLat <= 85 && w.damLon >= -180 && w.damLon <= 180)) errors.push('The dam location is not a valid latitude/longitude.');
  if (!w.bbox) errors.push('Enter the study-area bounds.');
  else {
    const [w0, s0, e0, n0] = w.bbox;
    if (!(w0 < e0 && s0 < n0)) errors.push('Study-area bounds must have west < east and south < north.');
    else if (!(w.damLon >= w0 && w.damLon <= e0 && w.damLat >= s0 && w.damLat <= n0)) errors.push('The dam location must fall inside the study-area bounds.');
  }
  return errors;
}

type Sourced = {value: unknown; unit: string; source: string; status: 'placeholder'};

function sv(value: unknown, unit: string, source: string): Sourced {
  const given = value !== null && value !== undefined && !(typeof value === 'number' && !Number.isFinite(value));
  return {value: given ? value : null, unit, source: given ? source : '', status: 'placeholder'};
}

/** The §3.1 site config (as JSON) for `POST /sites`. */
export function buildSiteConfig(w: WizardSite): Record<string, unknown> {
  const src = w.citation?.trim() || OPERATOR_SOURCE;
  const dam = [w.damLon, w.damLat];
  const b = w.breach;
  return {
    schema_version: 1,
    site: {id: siteIdFromName(w.name), name: w.name.trim()},
    crs: {utm_epsg: {value: utmEpsg(w.damLon, w.damLat), unit: 'epsg', source: 'derived: UTM zone of the entered dam location', status: 'placeholder'}},
    domains: {
      far_field: {
        bbox: sv(w.bbox, 'deg', src),
        grid_resolution: sv(null, 'm', src),
        inflow: {from: 'main_dam', location: sv(null, 'deg', src)},
      },
      near_field: {
        bbox: sv(null, 'deg', src),
        grid_resolution: sv(null, 'm', src),
        inflow: {from: 'far_field', location: sv(null, 'deg', src)},
      },
    },
    dams: [{
      id: 'main_dam',
      name: w.damName.trim(),
      kind: w.damKind,
      triggered_by: null,
      equations_applicable: w.damKind !== 'concrete_dam',
      location: sv(dam, 'deg', src),
      breach_location: sv(null, 'deg', src),
      breach_inputs: {
        water_volume_above_invert: sv(b.waterVolumeM3, 'm^3', src),
        water_height_above_invert: sv(b.waterHeightM, 'm', src),
        breach_height: sv(b.breachHeightM, 'm', src),
        dam_height: sv(b.damHeightM, 'm', src),
        average_embankment_width: sv(b.embankmentWidthM, 'm', src),
        dam_type: sv(b.damType, 'enum', src),
        failure_mode: sv(b.failureMode, 'enum', src),
        erodibility: sv(b.erodibility, 'enum', src),
      },
    }],
    points_of_interest: [],
    events: [],
  };
}

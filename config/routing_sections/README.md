# Reviewed M3 → M4 routing sections (Feature 17, decision E7)

One file per site, `config/routing_sections/<site_id>.yaml`, declares the cross-section through
which M3 (D-Flow FM) discharge is integrated to drive an M4 (DualSPHysics) near-field inlet. It
lives outside the site-config schema on purpose (`contracts/schemas/site_config.schema.json` is
unchanged). Loader and rules: `backend/m3_dflowfm/routing_section.py`.

**No site has a file yet.** None may be created by copying a guess: the section must come from a
surveyed / reviewed source listed in `docs/data_sources.md`.

```yaml
site_id: <site_id>                    # must match the file name
sections:
  - id: <slug>                        # e.g. lachen_chu_nearfield_inflow
    purpose: m3_to_m4_inflow          # exactly one entry with this purpose
    geometry:
      type: LineString
      coordinates: [[x0, y0], [x1, y1]]   # site UTM metres
      crs: EPSG:<site utm_epsg>           # must equal the site config's crs.utm_epsg
    extraction_method: <method id>    # see APPROVED_PRODUCTION_METHODS in routing_section.py
    source: src_NNN                   # docs/data_sources.md ID for the section geometry
    notes: <free text>
    approval:                         # filled in ONLY by a named reviewer, never by tooling
      approved_by: null
      approved_at: null
```

## What the file does and does not do

- **Presence approves nothing.** Production routing is READY only when ALL hold: the entry
  validates; `approval.approved_by` **and** `approval.approved_at` are filled; `extraction_method`
  is in `APPROVED_PRODUCTION_METHODS`; and the production gate's checklist item
  `m3_to_m4_routed_section` (`docs/real_input_checklist.json`) is READY.
- `APPROVED_PRODUCTION_METHODS` is **empty**: decision S2 (extraction variable / integration method)
  is deferred. So production routing is BLOCKED for every site until a method is approved and
  recorded in `docs/decisions.md`.
- In **controlled** (non-production) operation a declared section is used as declared even when
  unapproved, and is labelled so in the routed-discharge provenance. Without a file, controlled runs
  use the Feature 6 derived section (normal to the M1 centreline at `domains.near_field.inflow.location`,
  width `config/m4_sph.yaml` `routed_section_width_m`). Neither is ever a production section.

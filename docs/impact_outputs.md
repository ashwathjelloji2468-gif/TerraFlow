# Impact outputs — defaults (DRAFT)

Status: draft for team review. Each default has a **Why** so it can be changed deliberately.

**Implementation status (2026-10-02, Feature 9):** the header-block defaults now live in `config/impact.yaml`
(`impact:`) and drive `backend/m6_impact/impact.py` and `exports.py` for M5 (`gp_emulator`) queries: zones,
population range, depth classes, P10 isochrones, lead time. Not implemented: the D·V layer (thresholds still
unverified), the low-confidence one-zone downgrade (M5 persists no per-cell confidence), the scenario-mode
breach-pair rule (M5 runs one scenario; scenario-mode zones use the GP's own uncertainty, stated in outputs),
the village table and non-resident populations. §5's 'both factors are still placeholders' is historical:
`config/impact.yaml` now holds sourced FX/index/road-width values.
Related: `docs/m5_spec.md` (thresholds, query modes, confidence rule), `sites/<site_id>.yaml`
(points of interest).

All defaults below should live in config (e.g. `config/impact.yaml`), not in code:

```yaml
depth_classes_m:      [0.1, 0.3, 1.0, 2.0, 5.0]    # edges; below 0.1 = not shown
dv_hazard_m2s:        [0.5, 1.0, 3.0]              # depth x velocity edges (VERIFY source)
arrival_bands_min:    [15, 30, 60, 120, 180, 360]  # last band = "> 360"
arrival_percentile:   10                           # early arrival used for isochrones
detection_delay_min:  10                           # for "lead time after detection"
zone_high_p:          0.5                          # P(depth > 0.3 m) >= this -> HIGH
zone_possible_p:      0.1                          # >= this and < high -> POSSIBLE
population_sig_figs:  2
population_floor:     50                           # counts below this shown as "< 50"
```

---

## 1. Depth classes (maximum depth)

| Class | Depth | Meaning on the ground |
|---|---|---|
| Wet | 0.1–0.3 m | water has arrived, but it isn't counted as flooded |
| 1 Low | 0.3–1.0 m | walking and driving become unsafe; small vehicles can float |
| 2 Moderate | 1.0–2.0 m | ground floors flood; people can't wade out |
| 3 High | 2.0–5.0 m | single-storey houses are submerged; the roof is the only refuge |
| 4 Extreme | > 5 m | multi-storey buildings at risk; typical in gorge reaches |

**Why**
- 0.1 m and 0.3 m are the same as the arrival and extent thresholds in `m5_spec.md` §4, so all outputs agree.
- 1 m and 2 m mark jumps in what people can do: first they can't walk out, then they can't shelter at home.
- 5 m is needed because Himalayan GLOF depths in gorges are often 5–20 m. Stopping at "> 2 m" would treat a flooded ground floor the same as a house that has gone.

### 1.1 Depth × velocity hazard layer

Depth alone understates the danger in steep valleys: 1 m of water at 5 m/s is deadly. Show a
second layer, D·V = max depth × max velocity:

| D·V (m²/s) | Meaning |
|---|---|
| < 0.5 | lower hazard to people |
| 0.5–1.0 | unsafe for adults |
| 1.0–3.0 | unsafe for most people |
| > 3.0 | masonry buildings damaged |

**Why:** these are commonly cited ranges in the flood-hazard literature. **VERIFY and cite a source
before the demo.** They are not taken from a document we hold.

**Caveat:** max depth and max velocity may occur at different times, so their product slightly
overstates the hazard. That is acceptable for a warning map; document it.

### 1.2 Extra flags on the map

- **Bank-erosion strip:** a buffer along the channel in steep reaches (width to be decided by the team). *Why:*
  GLOFs carry heavy debris and undercut banks, so buildings just above the flood line can still collapse.
- **Low-confidence hatching:** cells with emulator confidence Low (`m5_spec.md` §6). *Why:* this shows the real
  uncertainty instead of hiding it.

---

## 2. Arrival isochrones

Bands, measured from breach start: **0–15 min, 15–30, 30–60, 1–2 h, 2–3 h, 3–6 h, > 6 h.**

**Why**
- The bands are uneven on purpose. Each one matches what responders can do in that time:
  - < 15 min: only immediate self-evacuation to high ground;
  - 15–30 min: siren or phone alert;
  - 30–60 min: organised evacuation;
  - hours: time for district administration action.
  Equal 30-minute bands would squeeze all the critical decisions into the first band.
- Isochrones use **early arrival (10th percentile)** in unknown-breach mode, not the median. A late
  warning costs lives; an early one costs inconvenience. This matches the deliberately early 0.1 m
  arrival threshold.
- Also show **lead time after detection** = arrival − `detection_delay_min`. Arrival from breach
  start overstates the time people really have. Lead time is the number operators will ask for.

In scenario mode there are no percentiles. Use the scenario's arrival time, and use the earlier of
the two breach-pair members when both are run.

---

## 3. Affected population as a range

### 3.1 Zones

Zones are based on P(depth > 0.3 m) from unknown-breach mode:

| Zone | Rule | Why |
|---|---|---|
| HIGH | P ≥ 0.5 | flooding is more likely than not |
| POSSIBLE | 0.1 ≤ P < 0.5 | keeps the tail visible; missing people at risk costs more than a false alarm |
| (not counted) | P < 0.1 | shown only as an outline |

- A cell with emulator confidence **Low** moves down one zone (HIGH → POSSIBLE). *Why:* those people
  are still in the upper number, but aren't presented as "likely".
- **Scenario mode** (no probabilities): HIGH = flooded under the *lower* breach estimate;
  HIGH+POSSIBLE = flooded under the *upper* breach estimate.

### 3.2 Presentation

- **Headline:** "About 1,200–3,400 people at risk". Lower = HIGH; upper = HIGH + POSSIBLE.
- **Don't headline the probability-weighted expected count.** It looks precise but hides the tail. Put it
  in a details panel if at all.
- **Round to 2 significant figures; show counts below 50 as "< 50".** *Why:* neither the census nor the model
  supports exact figures.
- **First row: people split by arrival band**, e.g. "HIGH with < 30 min lead time". *Why:* it is the most
  actionable number the tool produces.
- **Village table:** zone, earliest arrival, peak depth class and D·V class for each village. *Why:*
  district officials plan by village.
- **Critical facilities** (hospitals, bridges, dams, from `points_of_interest`) are reported separately,
  with zone and arrival, and are not folded into population.

### 3.3 Population data

- **Census 2011 village totals are points, not areas.** Disaggregate them to building footprints (OSM or
  Open Buildings) or to a gridded dataset (WorldPop or GHSL). *Why:* otherwise one point decides whether a
  whole village is in or out.
- **Label the data year and source on every output.** Either apply a growth factor or state
  "2011-based". *Why:* the census is 15 years old.
- **Add non-resident populations by hand, as separate lines:** hydropower labour camps, army posts,
  seasonal tourists and pilgrims, and highway traffic. *Why:* they are large in these valleys and missing
  from the census.
- Each added count follows the site-config convention: `value / unit / source / status`.

---

## 4. Acceptance checks

| # | Check | Pass |
|---|---|---|
| I1 | Class edges are read from config | changing the config changes the maps; no hard-coded edges |
| I2 | Population range ordering | lower ≤ upper always; both rounded as specified |
| I3 | Isochrones are conservative | the isochrone arrival is never later than the median arrival at any point of interest |
| I4 | Provenance shown | every population figure shows its data source and year; placeholder inputs are labelled |
| I5 | Low-confidence downgrade | a synthetic Low-confidence cell in HIGH is counted in the upper number only |

## 5. Loss estimation

`loss_inr` (docs/handoff_contract.md §4.7) prices flood damage at the P10/P50/P90 max-depth
maps using the JRC global flood depth-damage functions (Huizinga et al. 2017,
`docs/data_sources.md` src_031/src_032), the source `docs/ideation.md` itself suggested.
`backend/m6_impact/jrc_damage.py` extracts the curves/values into `damage_curves.csv` /
`asset_values.csv`; `backend/m6_impact/loss.py` computes `loss_inr`.

| Default | Value | Why |
|---|---|---|
| Damage curves | JRC ASIA continent column | JRC damage functions are continent-level, not country-level; ASIA is the only regional function that fits the Himalaya (Global is the only alternative, and it's a coarser fallback for continents JRC couldn't fit their own curve to). |
| Asset values | JRC India, building-based **Total** (€/m², 2010) | Max-damage values are country-level. "Building based / Total" (structure + content) is the JRC column that matches a per-footprint-area loss, rather than land-use or per-object measures. |
| Building class | OSM `building=*` tag → residential / commercial / industrial (`config/impact.yaml`'s `osm_building_to_jrc`); unmapped tags (including generic `building=yes`) → `default_building_class: residential` | JRC's curves are per damage class, not per OSM tag; a small explicit map keeps the mapping visible and editable instead of buried in code. Falling back to residential (not skipping) means every building is priced, honestly labelled as an assumption when the tag was unmapped. |
| Depth sample point | Building footprint centroid; road lines densified to the grid resolution | A footprint spans several cells in a steep valley; the centroid is a defensible single depth without needing zonal statistics. Roads are 1-D, so densified point sampling is the equivalent for a line. |
| Depth cap | 6.0 m, fraction 1.0 above it | The JRC table stops at 6 m; extrapolating past it would invent a curve shape the source doesn't support. Himalayan GLOF depths above 6 m are already "total loss" in practical terms. |
| Currency / price level | EUR (2010) → INR: FX rate first, then a 2010→current price index, both in `config/impact.yaml` | JRC values are 2010 EUR; the contract wants INR at today's prices. Both factors are still placeholders (no RBI reference rate or CPWD/WPI index chosen yet) — `loss_inr` stays a null Estimate, never an invented conversion (CLAUDE.md rule 3). |
| Scope | Only buildings and roads are priced; hospitals/schools/bridges (points in `facilities.gpkg`, no footprint) and agriculture (no cropland layer in `exposure/`) are not | Pricing a point asset would mean inventing a footprint area; skipping it honestly, and naming it in `assumptions`, is preferable to a fabricated number. |
| Range (`low`/`high`) | Sum of each percentile map's own per-cell loss, independently | The true P10-P90 of *total* loss would need per-sample Monte Carlo, not three summary maps. Summing each map on its own overstates the spread (every cell is treated as hitting its own percentile simultaneously) but is honest about *why* it's wider, not silently wrong. |

**Caveat, always stated:** JRC max-damage values are 2010 **national averages** for India. A
stone or timber-built house in a Himalayan village may cost quite differently to rebuild than
the country average this curve is built from — `loss_inr` is a rough estimate, not a precise
valuation, and every output says so in `assumptions`.

### Direct real-run MVP impact

For `delft3d_direct` query IDs without a persisted `impact.json`, M0 derives and
caches the §4.7 response from the query's real `depth_p50.tif`, the registered
run's history `timeseries.csv`, and the existing exposure files. A deterministic
single run has no HIGH/POSSIBLE uncertainty partition; its impacted exposure is
placed in the contract's POSSIBLE bucket, with that limitation explicit in the
caveats rather than reported as probability. Population is a spatial sum of the
stored WorldPop cells and has no interval. Economic loss remains null because
there is no run ensemble and approved FX/index/road-width values are unavailable.

For the Teesta MVP, the OSM files predate this adapter and their exact Overpass
query/fetch timestamp are not recorded. The exposed counts are intersections
with the pilot-domain raster only. The site-config bbox used to collect exposure
is placeholder-marked; therefore these are limited MVP counts, not a complete
site inventory. Missing cropland and hydropower layers remain unassessed.

### 5.1 Acceptance checks

| # | Check | Pass |
|---|---|---|
| I6 | No invented coefficients | `loss_inr` (and each `by_asset_class` entry) is a null Estimate whenever the EUR→INR rate, the price index, or (for roads) the road width is a placeholder — never a computed number built on a guessed value |
| I7 | Every number traces to a cited cell | every `asset_values.csv` row's `jrc_cell` matches the JRC workbook's actual sheet/cell for that value (`tests/m6_impact/test_jrc_damage.py`) |
| I8 | Range ordering | `low <= value <= high` on `loss_inr` and on every priced `by_asset_class` entry |
| I9 | Scope is stated, not silent | `assumptions` always names the unpriced classes (facilities, agriculture) and the placeholder factors currently in force |

## 6. Open decisions

1. Width of the bank-erosion strip, and whether it varies with slope.
2. The source for the D·V thresholds.
3. The detection delay per site (it depends on where sensors or observers are).
4. Growth factor vs "2011-based" labelling.
5. The EUR→INR rate (RBI reference rate, 2010 annual average) and the price index (CPWD cost
   index or WPI) for loss estimation — both still placeholders in `config/impact.yaml`.
6. Per-highway-class road widths (`config/impact.yaml`'s `default_road_width_m` is a single
   placeholder applied to every road) — e.g. from IRC geometric design standards.

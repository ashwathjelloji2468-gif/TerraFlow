# Data sources

Every fact in `sites/*.yaml`, `data/<site_id>/exposure/*.csv` and any other `SourcedValue`-shaped
input cites one of the IDs below in its `source` field. Add a new entry here before setting
`status: sourced` on a value that uses it (CLAUDE.md rule 3).

## src_031 — JRC global flood depth-damage functions (damage curves)

Huizinga, J., de Moel, H., Szewczyk, W. (2017). *Global flood depth-damage functions:
methodology and the database with guidelines.* European Commission Joint Research Centre,
EUR 28552 EN. doi: [10.2760/16510](https://doi.org/10.2760/16510).

Used for: `data/<site_id>/exposure/damage_curves.csv` (`backend/m6_impact/jrc_damage.py`),
the fractional depth-damage curves, ASIA continent column, 6 impact categories
(residential, commercial, industrial, transport, infrastructure-roads, agriculture),
0-6 m depth. Local copy: `data/copy_of_global_flood_depth-damage_functions__30102017.xlsx`
(gitignored, `data/` — not committed; re-download from the JRC data catalogue to reproduce).

## src_032 — JRC global flood depth-damage functions (max damage values)

Same source and workbook as src_031, its six `MaxDamage-*` sheets (country-level maximum
damage values, 2010 price level).

Used for: `data/<site_id>/exposure/asset_values.csv` (`backend/m6_impact/jrc_damage.py`),
India's max-damage values: `value_eur2010` per m2 (building-based Total for residential/
commercial/industrial; value-added/ha converted to per-m2 for agriculture; GDP-per-capita-
scaled for infrastructure-roads/transport, per each sheet's own documented method). Each
`asset_values.csv` row's `jrc_cell` names the exact sheet and cell(s) the number came from.

**Known limitation** (`docs/impact_outputs.md` §5): these are 2010 national averages for
India. Himalayan stone or timber-built houses may cost quite differently to rebuild; treat
`loss_inr` as a rough estimate, not a precise valuation.

The EUR->INR rate and the 2010->current price index needed to convert `value_eur2010` to
`value_inr_per_unit` are `src_047` and `src_050` below (both `status: sourced` in
`config/impact.yaml` as of 2026-09-28).

## src_047 — RBI reference rate, EUR/INR, calendar year 2010 annual average

Reserve Bank of India, *Handbook of Statistics on Indian Economy*, Table 139 "Exchange Rate
of the Indian Rupee vis-à-vis the SDR, US Dollar, Pound Sterling, D.M./Euro and Japanese
Yen (Calendar Year — Annual Average)": **60.6683 INR/EUR** for calendar year 2010.

**Verify before treating as final** — RBI's own Handbook PDF and Reference Rate Archive are
CAPTCHA-gated; this figure was read from Wikipedia's `Template:INRConvert/HistoricalRate`
dataset, which cites this same RBI Handbook table, not from the primary PDF directly.

Used for: `config/impact.yaml` `loss.eur_to_inr_2010` (`backend/m6_impact/jrc_damage.py`,
`backend/m6_impact/loss.py`) — the first of two factors converting JRC's 2010-EUR max-damage
values (src_032) to current INR.

## src_050 — Wholesale Price Index (WPI), All Commodities, Office of Economic Adviser (India)

Two PIB press releases, Office of the Economic Adviser, Ministry of Commerce & Industry,
Government of India:
- "Index Numbers of Wholesale Prices in India (Base: 2004-05=100), Review for the month of
  December, 2010" (released 2011-02-14): All-Commodities WPI = **144.1** (provisional).
- "Index Numbers of Wholesale Price in India for the Month of April, 2026 (Base Year:
  2011-12)": All-Commodities WPI = **167.0**.

The December-2010 figure is converted from base 2004-05=100 to base 2011-12=100 using the
official linking factor **1.873** (Office of Economic Adviser, `eaindustry.nic.in`, "Linking
Factor For WPI Series (Base: 2004-05)", All Commodities row): 144.1 / 1.873 = 76.94.
`price_index_2010_to_current` = 167.0 / 76.94 = **2.171**.

**Caveats:**
- Mixes a December-2010 point value with `src_047`'s full-2010 calendar-year average — both
  are within 2010, but not the same averaging convention.
- April 2026 is used as "current" rather than the true latest month (August 2026 = 110.8) —
  in September 2026 WPI's base was mid-revision from 2011-12=100 to 2022-23=100, and no
  reliable official 2011-12->2022-23 linking factor could be found (two lookups of what
  should be that factor gave inconsistent numbers). Chaining an uncertain third linking factor
  was judged worse than using a slightly older but cleanly single-sourced point. Re-derive
  once an official 2011-12->2022-23 linking factor is published.

Used for: `config/impact.yaml` `loss.price_index_2010_to_current` (`backend/m6_impact/jrc_damage.py`,
`backend/m6_impact/loss.py`) — the second of the two conversion factors, applied after `src_047`.

## src_051 — IRC:73, Geometric Design Standards for Rural (Non-Urban) Highways

Indian Roads Congress, *IRC:73*, carriageway width table: single-lane 3.75 m; two-lane
without raised kerbs 7.0 m; two-lane with raised kerbs 10.5 m; multi-lane 3.5 m per lane;
Village Roads restricted to 3.0 m normally (+1.5 m in areas with regular heavy-snowfall
clearance).

Used for: `config/impact.yaml` `loss.default_road_width_m` = 3.75 m (single-lane carriageway,
the IRC category closest to the residential/unclassified/track roads that dominate OSM
coverage in the Himalaya) — applied uniformly to every OSM `highway=*` value
(`backend/m6_impact/loss.py`). IRC:73 classifies roads by category (NH/SH/MDR/ODR/VR), not by
OSM tag, and gives a single width per category, not per class of road within it; a per-tag
mapping to IRC category (`docs/impact_outputs.md` §6, open decision) is not yet implemented —
this one width is applied to every road regardless of its `highway=*` tag.

## src_033 — SRTM GL1 (30 m), NASA JPL

NASA JPL (2013). *NASA Shuttle Radar Topography Mission Global 1 arc second [SRTMGL1].*
NASA EOSDIS Land Processes DAAC. doi: [10.5067/MEaSUREs/SRTM/SRTMGL1.003](https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003).
**Verify this DOI before setting `status: sourced` on anything citing it** — written from memory
this session, not fetched from the DOI resolver.

Used for: one of the DEM candidates `backend/m1_terrain/download.py` fetches for the M1-2 DEM
comparison report (`data/<site_id>/raw/dem_srtm_gl1.tif`). Vertical datum: EGM96 geoid (contract
§1.3). Not yet used for anything downstream — DEM choice happens after the comparison report.

## src_034 — Copernicus DEM GLO-30, ESA / Airbus

European Space Agency, Sinergise (2021). *Copernicus Global Digital Elevation Model, GLO-30.*
Distributed via OpenTopography. doi: [10.5270/ESA-c5d3d65](https://doi.org/10.5270/ESA-c5d3d65).
**Verify this DOI before setting `status: sourced`** — same caveat as src_033.

Used for: another DEM candidate (`data/<site_id>/raw/dem_copernicus_glo30.tif`). Vertical datum:
EGM2008 geoid (contract §1.3).

## src_035 — OpenTopography Global DEM API

Access route for src_033/src_034: `https://portal.opentopography.org/API/globaldem`
(`demtype=SRTMGL1|COP30`), authenticated with an API key
(`.env` `OPENTOPOGRAPHY_API_KEY`, CLAUDE.md rule 12 — never logged or committed). Not a dataset
citation on its own; always cite src_033/src_034 for the actual DEM.

## src_036 — ESA WorldCover 10 m v200 (2021)

Zanaga, D. et al. (2022). *ESA WorldCover 10 m 2021 v200.* doi:
[10.5281/zenodo.7254221](https://doi.org/10.5281/zenodo.7254221). Public COG tiles at
`s3://esa-worldcover/v200/2021/map/` (`eu-central-1`, no auth needed).
**Verify this DOI before setting `status: sourced`** — same caveat as src_033.

Used for: `data/<site_id>/raw/landcover_esa_worldcover.tif` (`backend/m1_terrain/download.py`),
a landcover candidate feeding M1's `roughness.tif` (Manning's n lookup by class).

## src_043 — Historical Teesta III project parameters (existing CFRD)

Government of India, Ministry of Environment, Forest and Climate Change. *Minutes of the 19th
Meeting of the Expert Appraisal Committee (River Valley and Hydroelectric Projects), Teesta
Stage III HE Project agenda/site-visit materials*, 2024. [PARIVESH document](https://parivesh.nic.in/utildoc/114429141_1733834917785.pdf).
Use the table column labelled **Existing Salient Features** for the historical 2023 dam: CFRD,
60 m maximum height above riverbed, FRL EL 1585 m, MDDL EL 1565 m, gross storage 5.08 MCM
(EL 1530–1585 m), live storage 3.33 MCM (EL 1565–1585 m), and catchment 2786.7 km². The same
document separately describes a proposed replacement concrete-gravity dam; that replacement is
not the 2023 structure and must not be used for the historical event.

## src_044 — Sikkim 2023 flood reconstruction and cascade impacts

Authors, *The Sikkim flood of October 2023: Drivers, causes and impacts of a multihazard cascade*.
[White Rose repository record and paper](https://eprints.whiterose.ac.uk/id/eprint/224098/).
The paper reports its reconstruction reaching Chungthang at about 00:30 IST on 4 October and a
modelled peak discharge of about 5340 m³/s there. It does **not** provide the complete observed
hydrograph needed by M3. These values differ from the 03:20 / 7355 m³/s MVP reconstruction
targets; the latter must remain explicitly imposed targets, not attributed to this source.

## src_045 — South Lhonak GLOF discharge reconstruction (2025)

Gaikwad, D., Tiwari, R.K. & Goswami, A. (2025). *Reconstruction of the 2023 South Lhonak Lake
outburst flood and modelling future scenarios in the Sikkim Himalaya*. Natural Hazards.
doi: [10.1007/s11069-025-07350-9](https://doi.org/10.1007/s11069-025-07350-9). The abstract reports
modelled (not observed) peak discharge of approximately 7355 m³/s at Chungthang for the actual
event reconstruction. This is one reconstruction result, not a complete measured hydrograph.

## src_046 — CWC preliminary Teesta basin incident report

Central Water Commission / National Dam Safety Authority, *Preliminary report on incident
occurred on 04.10.2023 in Teesta Basin of Sikkim*, in the [3rd NCDS meeting agenda pack](https://cwc.gov.in/sites/default/files/agenda-3rd-ncds-meeting.pdf).
Records the 4 October event in the Teesta basin and impacts to Teesta III. Use for incident
context only; it is not a complete discharge time series.

## src_048 — OpenStreetMap exposure extract

OpenStreetMap contributors, data under the Open Database License (ODbL). Attribution and
licence: [OpenStreetMap copyright and licence](https://www.openstreetmap.org/copyright).
Project extracts are stored in `data/<site_id>/exposure/{buildings,roads,facilities,places}.gpkg`.
The Teesta files present at MVP time were retained pre-existing extracts; their original fetch
timestamp and exact Overpass request are not recorded in `data/teesta/exposure/provenance.json`.
Treat coverage and currency as unknown; do not infer that omitted assets are absent.

## src_049 — WorldPop 2020 India, 1 km, UN-adjusted population counts

WorldPop, University of Southampton. *Global 2000–2020, 1 km, UN-adjusted population counts*.
[Dataset description](https://hub.worldpop.org/Global1_2000-2020) and [2020 India raster](https://data.worldpop.org/GIS/Population/Global_2000_2020_1km_UNadj/2020/IND/).
The stored Teesta raster derives from the 2020 India raster, is clipped and sum-preserving
resampled to the project's 30 m far-field grid, and uniformly disaggregates source-cell counts;
it is not building-level detail or a current census. License: CC BY 4.0.

## src_037 — CartoDEM, NRSC Bhoonidhi

National Remote Sensing Centre (ISRO), *CartoDEM* (version and release TBD — no public bulk-download
API; tiles must be requested/downloaded manually from Bhoonidhi, https://bhoonidhi.nrsc.gov.in).
**Version and vertical datum not yet confirmed** — do not set `status: sourced` on anything citing
this entry until both are filled in here (contract §1.3: "check CartoDEM's documentation").

Used for: the highest-resolution DEM candidate where available, mosaicked by
`backend/m1_terrain/download.py --cartodem-dir`.

## src_038 — Sentinel-2 MSI Level-2A (surface reflectance), Copernicus / ESA

Copernicus Sentinel-2 (processed by ESA). *MSI Level-2A BOA Reflectance Product.* European Space
Agency. Accessed via Google Earth Engine `COPERNICUS/S2_SR_HARMONIZED`. No single DOI — ESA's
recommended citation form is dataset + processor + agency, not a versioned paper. **Verify the
current recommended citation wording on sentinels.copernicus.eu before setting `status: sourced`**
— it has changed collection-to-collection (Collection 1 vs earlier).

Used for: `backend/m7_gee/provider.py` `s2_month` — monthly cloud-masked NDWI composites for
`lake_area.csv` (`method: s2_water_index`), masked by the SCL band's cloud/cloud-shadow/cirrus
classes (3, 8, 9, 10) and its snow/ice class (11).

## src_039 — Sentinel-1 GRD (C-band SAR), Copernicus / ESA

ESA/Copernicus. *Sentinel-1 Level-1 Ground Range Detected (GRD).* Accessed via Google Earth Engine
`COPERNICUS/S1_GRD`. No single citable DOI (a mission/processor citation, not a dataset paper) —
same caveat as src_038.

Used for: `backend/m7_gee/scene_search.py` (scene browsing) and `backend/m7_gee/provider.py`
`s1_month` — the cloud-free fallback for monthly lake-area classification (`method:
s1_threshold`), VV backscatter thresholded.

## src_040 — CHIRPS Daily (rainfall), Climate Hazards Center / UCSB

Funk, C. et al. (2015). *The climate hazards infrared precipitation with stations—a new
environmental record for monitoring extremes.* Scientific Data, 2, 150066. doi:
[10.1038/sdata.2015.66](https://doi.org/10.1038/sdata.2015.66). Accessed via Google Earth Engine
`UCSB-CHG/CHIRPS/DAILY`. **Verify this DOI before setting `status: sourced`** — same caveat as
src_033. Known limitation (CLAUDE.md "Known limitations"): satellite-IR-based rainfall estimates
like CHIRPS tend to underestimate orographic (high-mountain) precipitation in steep Himalayan
terrain — flagged as caveat `chirps_mountain_underestimate` wherever CHIRPS is used.

Used for: `backend/m7_gee/provider.py` `rainfall_daily` (default dataset, `GeeSettings.
rain_dataset = "chirps"`) — catchment-mean daily rainfall for `rainfall.csv`. `GPM_IMERG`
(`NASA/GPM_L3/IMERG_V07`, no separate entry here — a NASA, not ESA/UCSB, product) is the
alternative (`--rain-dataset gpm_imerg`).

## src_041 — HydroBASINS level 12, HydroSHEDS / WWF

Lehner, B., Grill, G. (2013). *Global river hydrography and network routing: baseline data and new
approaches to study the world's large river systems.* Hydrological Processes, 27(15), 2171–2186.
doi: [10.1002/hyp.9740](https://doi.org/10.1002/hyp.9740). Accessed via Google Earth Engine
`WWF/HydroSHEDS/v1/Basins/hybas_12`. **Verify this DOI before setting `status: sourced`** — same
caveat as src_033.

Used for: `backend/m7_gee/provider.py` `catchment` — the rainfall catchment (the HydroBASINS
level-12 basin containing the lake, plus every basin upstream of it via `NEXT_DOWN`) that
`rainfall.csv` is averaged over. `docs/decisions.md` "M7 GEE fetch" has the reasoning for why
HydroBASINS was picked over an M1-derived flow-accumulation catchment.

## src_042 — Chamoli 2021 rock–ice avalanche reconstruction

Shugar, D. H. et al. (2021). *A massive rock and ice avalanche caused the 2021 disaster at
Chamoli, Indian Himalaya.* Science, 373, eabh4455. doi:
[10.1126/science.abh4455](https://doi.org/10.1126/science.abh4455). Open author manuscript and
supplementary materials: <https://eprints.whiterose.ac.uk/id/eprint/175202/>.

Used for: `docs/events/chamoli_2021.md`'s event onset, source-volume estimate, flow-process
classification, and the reported discharge/velocity bounds near the Rishiganga and Tapovan
projects. The paper does not provide a machine-ready discharge hydrograph or a catchment-wide
observed flood-depth raster; see the event record for M3 limits.

## src_071 — Teesta 2023 observed flood/scour extent (project-digitized)

Project-derived dataset, digitized by the SENTRIQ team. File:
`data/teesta/observed/flood_extent_2023.geojson` (EPSG:4326). Produced by semi-automatic
change detection in Google Earth Engine on Copernicus Sentinel-2 Level-2A surface reflectance
(`COPERNICUS/S2_SR_HARMONIZED`), comparing cloud-masked median composites for
2022-10-10 to 2022-12-15 (pre) and 2023-10-10 to 2023-12-15 (post), then manually QA'd in
QGIS. Contains modified Copernicus Sentinel data (2022–2023).

Method: a pixel is flagged if it lies on the valley floor (MERIT Hydro HAND < <40> m;
Yamazaki, D. et al. (2019). *MERIT Hydro: A high-resolution global hydrography map based on
latest topography datasets.* Water Resources Research, 55, 5053–5073. doi:
[10.1029/2019WR024873](https://doi.org/10.1029/2019WR024873)) and shows NDVI loss > <0.15>,
or red-band brightening > <0.02> with post-event NDVI < 0.2, or open/turbid water
(MNDWI > 0, B8 < 0.25), excluding residual snow. Patches under <10> connected pixels are
removed. Manual QA deleted <describe: e.g. hillslope landslides not connected to the channel,
cloud edges, the drained South Lhonak lake bed>. Final extent: <N> polygons, <area> km².
Thresholds were set by visual comparison with the post-event imagery only, never against
model output.

Used for: validation only (IoU/F1/precision/recall of simulated vs observed extent in the
Validation tab). Never used for training, calibration, or parameter tuning. Limits: optical
imagery taken days to weeks after the event records the erosion/deposition footprint, not the
peak water extent, and snow/cloud leave gaps in the upper valley near the lake
(<note gaps>). It is not an official product; no Copernicus EMS or UNOSAT extent was found
for this event.

## src_072 — South Lhonak lake area before/after the GLOF (ISRO/NRSC)

ISRO / National Remote Sensing Centre (2023). Satellite-based study of the South Lhonak lake
outburst, released 4 October 2023. <Add the official ISRO/NRSC URL if you can find it;
otherwise cite a news report of the statement:>
<https://www.deccanherald.com/india/isro-conducts-satellite-image-based-studies-of-south-lhonak-lake-in-sikkim-2712863>.

Reports a lake area of 162.7 ha (17 Sep 2023) and 167.4 ha (28 Sep 2023), falling to about
60.3 ha (4 Oct 2023), i.e. about 105 ha drained. Used for: the reference figures shown beside
the project's own GEE lake-area series on the Monitoring page. Not used to tune the
project's water-mask thresholds.

## src_073 — Sikkim 2023 multihazard cascade reconstruction

Sattar, A. et al. (2025). *The Sikkim flood of October 2023: Drivers, causes, and impacts of
a multihazard cascade.* Science. doi:
[10.1126/science.ads2659](https://doi.org/10.1126/science.ads2659).

Used for: event narrative (moraine collapse of about 14.7 million m³, about 50 million m³
drained) and literature context for the Validation tab. <If this is already one of src_044 or
src_045, merge instead of adding a duplicate.>

## src_074 — Worldwide historical dam failure database (breach-equation test data)

Bernard-Garcia, M. & Mahdi, T. (2020). *A Worldwide Historical Dam Failure's Database.*
Scholars Portal Dataverse, V1. doi: [10.5683/SP2/E7Z09B](https://doi.org/10.5683/SP2/E7Z09B).
Licence: <check on the Dataverse page>.

Comparison paper for the expected error ranges: <authors — confirm on the article page>
(2026). *An update on data-fusion-based dam breach empirical equations based on a worldwide
historical dam failure database: a comparative assessment.* Natural Hazards. doi:
[10.1007/s11069-026-08239-x](https://doi.org/10.1007/s11069-026-08239-x).

Used for: `tests/data/breach_cases.csv`, built by `scripts/build_breach_cases.py`
(earthfill/rockfill cases only; incomplete rows dropped per equation), to check the M2
equations (F16, F8, F95, MCLM, Z20) against the paper's median percentage errors. Test data
only; not used at runtime.

## src_075 — GloFAS v4 daily river discharge, via the Open-Meteo Flood API

Global Flood Awareness System (GloFAS) v4, Copernicus Emergency Management Service: daily river
discharge on a ~0.05° (~5 km) grid, reanalysis from 1984 and archived/seamless forecasts after,
served by the Open-Meteo Flood API, https://flood-api.open-meteo.com/v1/flood (docs:
https://open-meteo.com/en/docs/flood-api; no API key for non-commercial use). Units m³/s.
**Modelled, not gauged.** Licence: GloFAS data CC BY 4.0 (Copernicus EMS) and Open-Meteo terms —
**verify both licence pages before setting any derived value to `status: sourced`.**

Used for: `backend/m1_terrain/discharge.py` (Feature 2) — `data/<site_id>/raw/discharge_glofas.csv`
at the site's inflow point, as hydrological context for choosing `domains.far_field.inflow.base_flow`
(never written into a site config automatically).


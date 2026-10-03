"""Site config model and loader for `sites/<site_id>.yaml` (schema v1).

The YAML layout is the one in `sites/template.yaml`. Every data value is a
SourcedValue: {value, unit, source, status[, note]}. A value missing `unit`,
`source` or `status` is rejected. `status: placeholder` values load, but the
loader warns loudly and lists them; every result built from such a config must
set `has_placeholders: true` (docs/handoff_contract.md §0 rule 5).

NOTE: docs/handoff_contract.md §3.1 (contract_version 0.3.0) mirrors this schema —
see docs/decisions.md "Site config: YAML v1 canonical, contract 0.3.0" (2026-09-25).
"""

from __future__ import annotations

import logging
import math
import os
import re
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

SITES_DIR = Path(__file__).resolve().parents[2] / "sites"

SITE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,31}$"  # contract §1.7
#: ISO 3166-1 alpha-3 style country code (three upper-case letters), e.g. "IND", "NPL". Feature 16.
COUNTRY_ISO3_PATTERN = r"^[A-Z]{3}$"
SLUG_PATTERN = r"^[a-z][a-z0-9_]*$"

#: `docs/decisions.md` "ID naming scheme" (2026-09-25): design | demo | historical | named extra.
SCENARIO_ID_SUFFIX_PATTERN = r"^(s\d{3}|demo_s\d{3}|hist_[a-z][a-z0-9_]*|n_[a-z][a-z0-9_]*)$"

Unit = Literal["m", "m^3", "deg", "epsg", "enum", "iso8601", "-", "m^0.5/s", "m^3/s", "s"]
Status = Literal["sourced", "placeholder"]


class SiteConfigError(ValueError):
    """A site config file is missing or fails validation."""


def format_validation_errors(exc: ValidationError) -> str:
    """Pydantic's error report WITHOUT the offending input values: `str(ValidationError)` echoes
    `input_value=...`, which would copy a mis-placed credential (e.g. an `api_key:` key someone
    added to a config) into logs, job errors and API responses. Location + message + type only."""
    lines = [f"{exc.error_count()} validation error(s)"]
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"{loc}\n  {err['msg']} [type={err['type']}]")
    return "\n".join(lines)


class PlaceholderWarning(UserWarning):
    """A loaded site config contains `status: placeholder` values."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =============================================================================
# SourcedValue and typed variants
# =============================================================================


class SourcedValue(_Strict):
    """A fact with its unit, source and status. `value` may be null only while placeholder."""

    value: float | int | str | list[float] | None
    unit: Unit
    source: str
    status: Status
    note: str | None = None

    @model_validator(mode="after")
    def _check_status(self):
        if self.value is None and self.status != "placeholder":
            raise ValueError(f"value is null but status is '{self.status}'; null is allowed only for placeholders")
        if self.status == "sourced" and not self.source.strip():
            raise ValueError("a sourced value needs a non-empty source (full citation)")
        return self


def _check_lon_lat(lon: float, lat: float) -> None:
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise ValueError(f"({lon}, {lat}) is not a valid [lon, lat] in degrees (longitude first)")


class PointValue(SourcedValue):
    """[lon, lat] in EPSG:4326 decimal degrees."""

    value: list[float] | None
    unit: Literal["deg"]

    @field_validator("value")
    @classmethod
    def _point(cls, v):
        if v is not None:
            if len(v) != 2:
                raise ValueError(f"a point must be [lon, lat], got {len(v)} numbers")
            _check_lon_lat(*v)
        return v


class BBoxValue(SourcedValue):
    """[min_lon, min_lat, max_lon, max_lat] in EPSG:4326 decimal degrees."""

    value: list[float] | None
    unit: Literal["deg"]

    @field_validator("value")
    @classmethod
    def _bbox(cls, v):
        if v is not None:
            if len(v) != 4:
                raise ValueError(f"a bbox must be [min_lon, min_lat, max_lon, max_lat], got {len(v)} numbers")
            _check_lon_lat(v[0], v[1])
            _check_lon_lat(v[2], v[3])
            if not (v[0] < v[2] and v[1] < v[3]):
                raise ValueError(f"bbox {v} must satisfy min_lon < max_lon and min_lat < max_lat")
        return v


class LengthValue(SourcedValue):
    value: Annotated[float, Field(gt=0)] | None
    unit: Literal["m"]


class VolumeValue(SourcedValue):
    value: Annotated[float, Field(gt=0)] | None
    unit: Literal["m^3"]


class EpsgValue(SourcedValue):
    """A WGS 84 / UTM EPSG code (326nn north, 327nn south)."""

    value: int | None
    unit: Literal["epsg"]

    @field_validator("value")
    @classmethod
    def _utm(cls, v):
        if v is not None and not (32601 <= v <= 32660 or 32701 <= v <= 32760):
            raise ValueError(f"EPSG:{v} is not a WGS 84 / UTM zone (32601-32660, 32701-32760)")
        return v


class DamTypeValue(SourcedValue):
    value: Literal["HD", "CD", "FD", "ZD"] | None  # homogeneous | core-wall | concrete-faced | zoned-fill
    unit: Literal["enum"]


class FailureModeValue(SourcedValue):
    value: Literal["O", "P"] | None  # overtopping | piping
    unit: Literal["enum"]


class ErodibilityValue(SourcedValue):
    value: Literal["H", "M", "L"] | None
    unit: Literal["enum"]


class DimensionlessValue(SourcedValue):
    value: float | None
    unit: Literal["-"]


class WeirCoefficientValue(SourcedValue):
    """A broad-crested weir coefficient, m^0.5/s (`backend/m2_breach/weir.py`)."""

    value: Annotated[float, Field(gt=0)] | None
    unit: Literal["m^0.5/s"]


class CurveValue(SourcedValue):
    """A surveyed elevation-volume curve: [[elevation_m, volume_m3], ...]."""

    value: list[list[float]] | None
    unit: Literal["m^3"]

    @field_validator("value")
    @classmethod
    def _curve(cls, v):
        if v is not None:
            if len(v) < 2:
                raise ValueError("a surveyed curve needs at least 2 points")
            for point in v:
                if len(point) != 2:
                    raise ValueError(f"each curve point must be [elevation_m, volume_m3], got {point!r}")
            elevations = [p[0] for p in v]
            volumes = [p[1] for p in v]
            if elevations != sorted(elevations) or len(set(elevations)) != len(elevations):
                raise ValueError("curve elevations must be strictly increasing")
            if volumes != sorted(volumes) or len(set(volumes)) != len(volumes):
                raise ValueError("curve volumes must be strictly increasing")
        return v


class DischargeValue(SourcedValue):
    value: Annotated[float, Field(gt=0)] | None
    unit: Literal["m^3/s"]


class _RangeValue(SourcedValue):
    """[low, high] in the subclass's unit, low <= high, both >= 0. Used for
    `Dam.imposed_ranges` (`docs/decisions.md`, `docs/handoff_contract.md` §3.1
    `imposed_ranges`) — a dam with `equations_applicable: false` (e.g. a
    concrete dam) reports these instead of computed method ranges."""

    value: list[float] | None

    @field_validator("value")
    @classmethod
    def _range(cls, v):
        if v is not None:
            if len(v) != 2:
                raise ValueError(f"a range must be [low, high], got {len(v)} numbers")
            lo, hi = v
            if lo < 0 or hi < 0:
                raise ValueError(f"range {v} must be non-negative")
            if lo > hi:
                raise ValueError(f"range {v} must satisfy low <= high")
        return v


class DischargeRangeValue(_RangeValue):
    unit: Literal["m^3/s"]


class LengthRangeValue(_RangeValue):
    unit: Literal["m"]


class TimeRangeValue(_RangeValue):
    unit: Literal["s"]


class DateTimeValue(SourcedValue):
    """ISO 8601 date or datetime, kept as the original string."""

    value: str | None
    unit: Literal["iso8601"]

    @field_validator("value", mode="before")
    @classmethod
    def _iso(cls, v):
        if isinstance(v, (date, datetime)):  # unquoted YAML dates arrive as date objects
            v = v.isoformat()
        if v is not None:
            try:
                datetime.fromisoformat(v)
            except (TypeError, ValueError):
                raise ValueError(f"{v!r} is not an ISO 8601 date/datetime") from None
        return v


# =============================================================================
# Structure (mirrors sites/template.yaml)
# =============================================================================


class Site(_Strict):
    id: Annotated[str, Field(pattern=SITE_ID_PATTERN)]
    name: str
    region: str | None = None
    river: str | None = None
    #: Optional (Feature 16). Country of the study area, used where a national dataset is selected
    #: (WorldPop). Never inferred from `region` or the bbox; null means "not stated".
    country_iso3: Annotated[str, Field(pattern=COUNTRY_ISO3_PATTERN)] | None = None


class Crs(_Strict):
    utm_epsg: EpsgValue


class Inflow(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")  # a dam id, or "far_field" for the near-field domain
    location: PointValue
    base_flow: DischargeValue | None = None  # steady discharge already in the channel before t0


class Domain(_Strict):
    description: str | None = None
    bbox: BBoxValue
    grid_resolution: LengthValue
    inflow: Inflow


class Domains(_Strict):
    far_field: Domain
    near_field: Domain


class BreachInputs(_Strict):
    water_volume_above_invert: VolumeValue
    water_height_above_invert: LengthValue
    breach_height: LengthValue
    dam_height: LengthValue
    average_embankment_width: LengthValue
    dam_type: DamTypeValue
    failure_mode: FailureModeValue
    erodibility: ErodibilityValue


class VolumeElevation(_Strict):
    """Storage above the final breach invert (`backend/m2_breach/storage.py`).

    Optional and additive (`docs/decisions.md`, `docs/handoff_contract.md` §3.1); a `Dam` without
    this block cannot use the `breach_growth_weir` hydrograph method (`hydrograph.py` falls back
    to `triangular`).
    """

    method: Literal["surveyed_curve", "area_volume_relation"]
    breach_invert_elevation_m: LengthValue | None = None
    points: CurveValue | None = None
    area_volume_exponent_b: DimensionlessValue | None = None

    @model_validator(mode="after")
    def _check_method(self):
        if self.method == "surveyed_curve":
            if self.points is None or self.breach_invert_elevation_m is None:
                raise ValueError("volume_elevation.method='surveyed_curve' needs 'points' and "
                                  "'breach_invert_elevation_m'")
        else:
            if self.area_volume_exponent_b is None:
                raise ValueError("volume_elevation.method='area_volume_relation' needs "
                                  "'area_volume_exponent_b'")
            b = self.area_volume_exponent_b.value
            if b is not None and b <= 1:
                raise ValueError(f"area_volume_exponent_b must be > 1 (V = a*A^b), got {b!r}")
        return self


class BreachHydrographSettings(_Strict):
    """Weir coefficients and breach side slope (`backend/m2_breach/weir.py`).

    Optional and additive; no defaults exist in code (`docs/decisions.md`) — a `Dam` without this
    block cannot use the `breach_growth_weir` hydrograph method.
    """

    weir_coefficient_rect: WeirCoefficientValue
    weir_coefficient_side: WeirCoefficientValue
    side_slope_z: DimensionlessValue  # horizontal:vertical, >= 0

    @field_validator("side_slope_z")
    @classmethod
    def _side_slope(cls, v):
        if v.value is not None and v.value < 0:
            raise ValueError(f"side_slope_z must be >= 0, got {v.value!r}")
        return v


class ImposedRanges(_Strict):
    """Breach parameter ranges imposed from the site config rather than computed from the
    Azmi (2026) equations — required when `Dam.equations_applicable` is false (contract §3.1,
    §4.2: "Dams with `equations_applicable: false` return `imposed_ranges` from the config,
    flagged with caveat `concrete_dam_imposed`"). `docs/Equations.md` §7 refuses `kind:
    concrete_dam`; those dams use this block instead of being blocked outright."""

    peak_discharge_m3s: DischargeRangeValue
    breach_width_m: LengthRangeValue
    failure_time_s: TimeRangeValue


class CascadeTrigger(_Strict):
    """When a downstream dam in a cascade fails, under `Cascade.approach ==
    'two_stage_imposed'` (`docs/decisions.md`): the dam's own breach hydrograph is triggered
    once routed inflow from its upstream dam(s) first reaches `value` (m^3/s). Per-dam, not
    site-level, because different dams in a chain have different capacities — this is additive
    to contract §3.1's single site-level `cascade.trigger` and is PENDING team agreement
    (`docs/decisions.md`)."""

    type: Literal["inflow_threshold"]
    value: DischargeValue


class Dam(_Strict):
    id: Annotated[str, Field(pattern=SLUG_PATTERN)]
    name: str
    kind: Literal["moraine_dammed_lake", "embankment_dam", "concrete_dam", "landslide_dam"]
    triggered_by: str | None = None
    trigger: CascadeTrigger | None = None
    equations_applicable: bool = True
    imposed_ranges: ImposedRanges | None = None
    location: PointValue
    breach_location: PointValue
    breach_inputs: BreachInputs
    volume_elevation: VolumeElevation | None = None
    breach_hydrograph: BreachHydrographSettings | None = None
    initial_water_level: LengthValue | None = None  # m, at t0; reservoirs inside the Delft3D domain (contract §3.1, §3.3)


class PointOfInterest(_Strict):
    id: Annotated[str, Field(pattern=SLUG_PATTERN)]
    name: str
    category: Literal["village", "dam", "bridge", "hospital"]
    location: PointValue


class Event(_Strict):
    id: Annotated[str, Field(pattern=SLUG_PATTERN)]
    name: str
    kind: Literal["historical", "hypothetical"]
    onset: DateTimeValue
    breach_times: dict[str, DateTimeValue]
    simulation_start: DateTimeValue
    simulation_end: DateTimeValue
    imagery_pre_event: DateTimeValue
    imagery_post_event: DateTimeValue


class Cascade(_Strict):
    """Present only when a site has a dam with `triggered_by` set (`docs/decisions.md` M2
    cascade decision). `approach='two_stage_imposed'` is the only one M2 implements today:
    `dambreak_structure` (dynamic breaching inside a Delft3D structure) belongs to M3, not M2 —
    `backend/m2_breach/cascade.py` raises if it sees that value."""

    approach: Literal["two_stage_imposed", "dambreak_structure"]


class SphSimulation(_Strict):
    """Which of this site's scenarios also get a near-field DualSPHysics run
    (docs/handoff_contract.md §4.4: `simulation.sph.scenarios`), so the matching Delft3D run's
    `summary_nearfield/*.tif` is computed over the same `sim_duration_s` window for a like-with-like
    `sph_vs_delft3d` comparison. Solver settings themselves are NOT here — they stay project-maintained
    in `config/m4_sph.yaml` (`backend/m4_sph/settings.py`: "none of it belongs in sites/*.yaml"),
    since they're engineering defaults, not a site fact."""

    scenarios: list[str] = []


class Simulation(_Strict):
    """`docs/handoff_contract.md` line ~422 flags this whole block as not yet added to
    `SiteConfig`; only the `sph.scenarios` piece is implemented so far. `delft3d` settings are
    deferred — `backend/m3_delft3d` doesn't exist yet, so there's nothing real to consolidate."""

    sph: SphSimulation = SphSimulation()


def _bbox_contains(outer: list[float], inner: list[float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _duplicates(ids: list[str]) -> list[str]:
    return sorted({i for i in ids if ids.count(i) > 1})


class EmulatorInputRange(_Strict):
    """Contract §3.3 `emulator_inputs[].range`. Like every config fact it carries a source and a
    status; a range that is not sourced stays `placeholder` and flags results (CLAUDE.md rule 3)."""

    low: Annotated[float, Field(gt=0)] | None
    high: Annotated[float, Field(gt=0)] | None
    unit: str
    basis: str | None = None
    source: str
    status: Status = "placeholder"

    @model_validator(mode="after")
    def _check(self):
        if self.low is not None and self.high is not None and not self.low < self.high:
            raise ValueError(f"emulator_inputs range must have low < high, got [{self.low}, {self.high}]")
        if self.status == "sourced" and (self.low is None or self.high is None or not self.source.strip()):
            raise ValueError("a sourced emulator_inputs range needs both bounds and a non-empty source")
        return self


class EmulatorInputSlider(_Strict):
    positions: list[int]
    mapping: Literal["linear", "log"]


#: Contract §3.3 reserved input names, with the unit each range must use.
EMULATOR_INPUT_UNITS = {"water_volume_m3": "m^3", "initial_water_level_m": "m", "breach_width_m": "m",
                        "failure_time_s": "s", "manning_multiplier": "-"}


class EmulatorInput(_Strict):
    """Contract §3.3 `emulator_inputs` entry. Feature 4 uses only `water_volume_m3` (F4-D5:
    volume is sampled only over an explicit range here); the other names are accepted and kept
    for later features."""

    name: Literal["water_volume_m3", "initial_water_level_m", "breach_width_m", "failure_time_s", "manning_multiplier"]
    dam_id: Annotated[str, Field(pattern=SLUG_PATTERN)] | None = None
    range: EmulatorInputRange
    slider: EmulatorInputSlider | None = None
    default: float | None = None

    @model_validator(mode="after")
    def _unit(self):
        want = EMULATOR_INPUT_UNITS[self.name]
        if self.range.unit != want:
            raise ValueError(f"emulator_inputs '{self.name}' range unit must be '{want}', got '{self.range.unit}'")
        if self.name != "manning_multiplier" and self.dam_id is None:
            raise ValueError(f"emulator_inputs '{self.name}' needs a dam_id")
        return self


class SiteConfig(_Strict):
    schema_version: Literal[1]
    site: Site
    crs: Crs
    domains: Domains
    dams: Annotated[list[Dam], Field(min_length=1)]  # upstream -> downstream
    cascade: Cascade | None = None
    points_of_interest: list[PointOfInterest] = []
    events: list[Event] = []
    simulation: Simulation = Field(default_factory=Simulation)
    emulator_inputs: list[EmulatorInput] = []  # contract §3.3 (Feature 4)

    @model_validator(mode="after")
    def _cross_checks(self):
        dam_ids = [d.id for d in self.dams]
        for item in self.emulator_inputs:
            if item.dam_id is not None and item.dam_id not in dam_ids:
                raise ValueError(f"emulator_inputs '{item.name}' dam_id '{item.dam_id}' is not a dam in this site")
        seen = [(i.name, i.dam_id) for i in self.emulator_inputs]
        if dups := _duplicates([f"{n}:{d}" for n, d in seen]):
            raise ValueError(f"duplicate emulator_inputs entries: {', '.join(dups)}")
        for label, ids in (("dam", dam_ids),
                           ("point_of_interest", [p.id for p in self.points_of_interest]),
                           ("event", [e.id for e in self.events])):
            if dups := _duplicates(ids):
                raise ValueError(f"duplicate {label} id(s): {', '.join(dups)}")

        for i, dam in enumerate(self.dams):
            if dam.triggered_by is not None and dam.triggered_by not in dam_ids[:i]:
                raise ValueError(f"dams[{i}].triggered_by '{dam.triggered_by}' is not a dam id listed "
                                 f"before it (dams are ordered upstream -> downstream)")
            if dam.triggered_by is None and dam.trigger is not None:
                raise ValueError(f"dam '{dam.id}': 'trigger' is only meaningful on a dam with "
                                 f"'triggered_by' set")
            if dam.triggered_by is not None and self.cascade is None:
                raise ValueError(f"dam '{dam.id}' has 'triggered_by' set but the site has no "
                                 f"top-level 'cascade' block")
            if (dam.triggered_by is not None and self.cascade is not None
                    and self.cascade.approach == "two_stage_imposed" and dam.trigger is None):
                raise ValueError(f"dam '{dam.id}': cascade.approach='two_stage_imposed' requires "
                                 f"every triggered dam to have its own 'trigger'")
            if dam.kind == "concrete_dam" and dam.equations_applicable:
                raise ValueError(f"dam '{dam.id}': kind='concrete_dam' requires "
                                 f"equations_applicable: false (docs/Equations.md §7 — the "
                                 f"breach equations are not recommended for concrete dams)")
            if not dam.equations_applicable and dam.imposed_ranges is None:
                raise ValueError(f"dam '{dam.id}': equations_applicable: false requires "
                                 f"'imposed_ranges'")

        ff, nf = self.domains.far_field, self.domains.near_field
        if ff.inflow.from_ not in dam_ids:
            raise ValueError(f"domains.far_field.inflow.from '{ff.inflow.from_}' must be a dam id")
        if nf.inflow.from_ not in [*dam_ids, "far_field"]:
            raise ValueError(f"domains.near_field.inflow.from '{nf.inflow.from_}' must be a dam id or 'far_field'")

        for event in self.events:
            if unknown := sorted(set(event.breach_times) - set(dam_ids)):
                raise ValueError(f"events '{event.id}' breach_times has unknown dam id(s): {', '.join(unknown)}")

        scenario_prefix = f"{self.site.id}__"
        for scenario_id in self.simulation.sph.scenarios:
            if not scenario_id.startswith(scenario_prefix):
                raise ValueError(f"simulation.sph.scenarios '{scenario_id}' must start with "
                                 f"'{scenario_prefix}' (docs/decisions.md ID naming scheme)")
            suffix = scenario_id[len(scenario_prefix):]
            if not re.match(SCENARIO_ID_SUFFIX_PATTERN, suffix):
                raise ValueError(f"simulation.sph.scenarios '{scenario_id}' does not match a known "
                                 f"scenario_id shape (docs/decisions.md ID naming scheme: "
                                 f"s<NNN> | demo_s<NNN> | hist_<event_id> | n_<slug>)")

        if ff.bbox.value is not None and nf.bbox.value is not None and not _bbox_contains(ff.bbox.value, nf.bbox.value):
            raise ValueError(f"near_field bbox {nf.bbox.value} must lie inside far_field bbox {ff.bbox.value}")

        far_cs, near_cs = ff.grid_resolution.value, nf.grid_resolution.value
        if far_cs is not None and near_cs is not None:
            ratio = far_cs / near_cs
            if near_cs > far_cs or not math.isclose(ratio, round(ratio), abs_tol=1e-9):
                raise ValueError(f"near_field grid_resolution ({near_cs} m) must divide far_field "
                                 f"grid_resolution ({far_cs} m) exactly")

        for dam in self.dams:
            hb, hd = dam.breach_inputs.breach_height.value, dam.breach_inputs.dam_height.value
            if hb is not None and hd is not None and hb > hd:
                warnings.warn(f"dam '{dam.id}': breach_height ({hb} m) exceeds dam_height ({hd} m)",
                              UserWarning, stacklevel=2)
        return self

    @property
    def placeholder_fields(self) -> list[str]:
        """Dotted paths of every `status: placeholder` value, in file order."""
        return list(_placeholder_paths(self, ""))

    @property
    def has_placeholders(self) -> bool:
        return bool(self.placeholder_fields)


def _placeholder_paths(obj: Any, path: str) -> Iterator[str]:
    if isinstance(obj, SourcedValue):
        if obj.status == "placeholder":
            yield path
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from _placeholder_paths(getattr(obj, name), f"{path}.{name}" if path else name)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _placeholder_paths(item, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for key, item in obj.items():
            yield from _placeholder_paths(item, f"{path}.{key}")


# =============================================================================
# Loader
# =============================================================================


def _resolve_path(path_or_id: str | Path, sites_dir: str | Path | None) -> Path:
    p = Path(path_or_id)
    if p.suffix in (".yaml", ".yml"):
        return p
    if sites_dir is not None:
        return Path(sites_dir) / f"{path_or_id}.yaml"
    # Default lookup: the repo's bundled `sites/<id>.yaml` first (so a bundled site can never be
    # shadowed), then a site onboarded through `POST /sites`, persisted at
    # `<data dir>/<id>/config/<id>.yaml` (contract §1.8; `SIH26_DATA_DIR` overrides the data dir,
    # the same variable `backend/m0_api/registry.py` reads).
    bundled = SITES_DIR / f"{path_or_id}.yaml"
    if bundled.is_file():
        return bundled
    onboarded = onboarded_config_path(str(path_or_id))
    return onboarded if onboarded.is_file() else bundled


def default_data_dir() -> Path:
    """`$SIH26_DATA_DIR`, else `<repo>/data` (contract §1.8)."""
    return Path(os.environ.get("SIH26_DATA_DIR", SITES_DIR.parent / "data"))


def onboarded_config_path(site_id: str, data_dir: str | Path | None = None) -> Path:
    """Where an onboarded site's config is persisted: `<data dir>/<site_id>/config/<site_id>.yaml`."""
    return Path(data_dir or default_data_dir()) / site_id / "config" / f"{site_id}.yaml"


def load_site_config(path_or_id: str | Path, sites_dir: str | Path | None = None) -> SiteConfig:
    """Load and validate a site config.

    `path_or_id` is a path to a .yaml file or a site id looked up in `sites_dir`
    (default: the repo's `sites/`, then the onboarded config under the data dir -- see
    `_resolve_path`). Raises FileNotFoundError or SiteConfigError.
    Emits one PlaceholderWarning (and a log warning) listing every placeholder.
    """
    path = _resolve_path(path_or_id, sites_dir)
    if not path.is_file():
        raise FileNotFoundError(f"site config not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise SiteConfigError(f"{path}: expected a YAML mapping at the top level")

    try:
        cfg = SiteConfig.model_validate(raw)
    except ValidationError as e:
        raise SiteConfigError(f"{path}: invalid site config\n{format_validation_errors(e)}") from None

    if cfg.site.id != path.stem:
        raise SiteConfigError(f"{path}: site.id '{cfg.site.id}' must equal the file name '{path.stem}'")

    placeholders = cfg.placeholder_fields
    if placeholders:
        bar = "!" * 78
        banner = (f"\n{bar}\n{path}: {len(placeholders)} PLACEHOLDER value(s) — NOT for real results.\n"
                  f"Every result built from this config must set has_placeholders=true.\n"
                  + "\n".join(f"  - {p}" for p in placeholders) + f"\n{bar}")
        logger.warning(banner)
        # The full field list is already in the log banner above; the warning itself stays a
        # single line so it doesn't get printed twice (once per sink) at full length.
        warnings.warn(f"{path.name}: {len(placeholders)} placeholders (has_placeholders=true; "
                       f"see log for the field list)", PlaceholderWarning, stacklevel=2)
    return cfg

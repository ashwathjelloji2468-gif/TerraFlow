"""Slider position -> exact M5 input (Feature 8), per `docs/handoff_contract.md` §3.3:

    linear: value = low + (p / 10) × (high − low)
    log:    the same on ln(value), i.e. value = exp(ln low + (p / 10) × (ln high − ln low))

`p` is the request's `position` in [0, 10] (`flood_query_request.schema.json`). `low`/`high` are
the persisted emulator's own training-design bounds (`InputSpec.low/high`), so every slider
position maps inside the trained box -- sliders cannot extrapolate. The mapping is the site
config's `emulator_inputs[].slider.mapping` when one is configured for that input; otherwise it
follows the emulator's own input scaling (`log10` -> `log`, `linear` -> `linear`), so equal slider
steps are equal steps in the space the GP was trained in.
"""
from __future__ import annotations

import math

from backend.m5_emulator.inputs import InputSpec

POSITION_MIN, POSITION_MAX = 0.0, 10.0
MAPPING_VERSION = "contract §3.3 slider mapping (linear | log on ln(value)), positions [0, 10]"


class SliderError(ValueError):
    """A slider position or mapping that cannot be applied."""


def default_mapping(spec: InputSpec) -> str:
    return "log" if spec.scaling == "log10" else "linear"


def slider_to_value(position: float, low: float, high: float, mapping: str) -> float:
    """§3.3 formula. Raises `SliderError` for a position outside [0, 10] or a bad mapping."""
    if not math.isfinite(position) or not POSITION_MIN <= position <= POSITION_MAX:
        raise SliderError(f"slider position {position} is outside [{POSITION_MIN:g}, {POSITION_MAX:g}]")
    if not low < high:
        raise SliderError(f"slider range needs low < high, got [{low}, {high}]")
    f = position / POSITION_MAX
    if mapping == "linear":
        return low + f * (high - low)
    if mapping == "log":
        if low <= 0:
            raise SliderError(f"log slider mapping needs low > 0, got {low}")
        return math.exp(math.log(low) + f * (math.log(high) - math.log(low)))
    raise SliderError(f"unknown slider mapping {mapping!r} (expected 'linear' or 'log')")


def map_slider(spec: InputSpec, position: float, configured_mapping: str | None = None) -> dict:
    """One slider -> its exact value plus the record kept in provenance."""
    mapping = configured_mapping or default_mapping(spec)
    value = slider_to_value(float(position), spec.low, spec.high, mapping)
    # Guard float round-off at the ends so the mapped value never leaves the trained box.
    value = min(max(value, spec.low), spec.high)
    return {"position": float(position), "mapping": mapping,
            "mapping_basis": "site config emulator_inputs[].slider.mapping" if configured_mapping
            else f"emulator input scaling '{spec.scaling}'",
            "low": spec.low, "high": spec.high, "range_basis": "persisted emulator training-design box",
            "value": value}

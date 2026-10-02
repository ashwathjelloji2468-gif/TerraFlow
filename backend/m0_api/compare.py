"""M0's half of Compare's "emulator vs physics" / "GP vs linear" sections
(`docs/handoff_contract.md` §5.6, route #15): reads what
`backend.m5_emulator.compare.write_compare_inputs` wrote under
`data/<site_id>/emulator/<model>/validation/compare/`, and renders/caches the
depth-difference PNG with M0-5 (`rendering.py`).

`sph_vs_delft3d` has no real data source anywhere in this codebase yet: it
needs a *paired* run -- `summary_nearfield/*.tif` for both `<scenario_id>__
delft3d` and `<scenario_id>__sph` under `data/<site_id>/runs/` (contract
§4.4/§1.8) -- and no code computes the depth-diff/arrival-probe metrics from
that pair even when both exist (that's the SPH-vs-D-Flow comparison task,
gated on a validated SPH near-field run). `build_response` therefore always
reports `sph_vs_delft3d.available: false`, naming the exact paths a real
comparison would need, instead of decorating a real `emulator_vs_physics`
sidecar with the contract example's fictitious zero-metric SPH numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.m0_api import registry, rendering

#: contract §1.7 model enum -- tried in order when resolving a scenario_id
#: to a held-out run_id, since the request only names the scenario.
MODELS = ("delft3d", "sph")

DIFF_LAYER_ID = "depth_diff"


def find_compare_sidecar(site_id: str, scenario_id: str | None) -> tuple[str, str, Path] | None:
    """`(model, held_out_run_id, sidecar_path)` for the first model that has
    one written, or `None` (falls back to the mock -- including when no
    `scenario_id` was given, since a run_id can't be resolved without one)."""
    if not scenario_id:
        return None
    for model in MODELS:
        held_out_run_id = f"{scenario_id}__{model}"
        sidecar_path = registry.data_dir() / site_id / "emulator" / model / "validation" / "compare" / f"{held_out_run_id}.json"
        if sidecar_path.is_file():
            return model, held_out_run_id, sidecar_path
    return None


def _sph_vs_delft3d_unavailable(site_id: str, scenario_id: str) -> dict:
    """Honest `available: false` state naming the two real inputs (contract
    §4.4 `summary_nearfield/*.tif`) a paired SPH/D-Flow FM comparison needs --
    neither exists for any scenario yet, and no code computes the diff even
    when they do."""
    needed = [
        f"data/{site_id}/runs/{scenario_id}__delft3d/summary_nearfield/",
        f"data/{site_id}/runs/{scenario_id}__sph/summary_nearfield/",
    ]
    return {
        "available": False, "domain": "nearfield", "time_window_s": 0,
        "metrics": {}, "probes": [], "layers": [],
        "run_ids": [], "unavailable_reason": (
            "No paired SPH/D-Flow FM near-field run exists for this scenario, and no comparison "
            f"pipeline computes the diff yet. Needed: {' and '.join(needed)}"
        ),
    }


def build_response(site_id: str, scenario_id: str, model: str, held_out_run_id: str, sidecar_path: Path) -> dict:
    """The full `Compare` dict: `emulator_vs_physics`/`gp_vs_linear` real from
    the sidecar, `sph_vs_delft3d` an honest unavailable state (see module
    docstring)."""
    sidecar = json.loads(sidecar_path.read_text())

    diff_url = f"/api/v1/files/{site_id}/emulator/{model}/validation/compare/{held_out_run_id}__depth_diff.png"
    return {
        "site_id": site_id, "scenario_id": scenario_id,
        "sph_vs_delft3d": _sph_vs_delft3d_unavailable(site_id, scenario_id),
        "emulator_vs_physics": {
            "available": True, "held_out_run_id": held_out_run_id, "metrics": sidecar["metrics"],
            "layers": [{
                "layer_id": DIFF_LAYER_ID, "type": "raster_png", "url": diff_url,
                "bounds_latlng": sidecar["bounds_latlng"], "style_id": "depth_diff", "unit": "m", "available": True,
            }],
        },
        "gp_vs_linear": sidecar["gp_vs_linear"],
        "when_to_use_key": "when_to_use_emulator_vs_physics_only",
        "caveats": sidecar["caveats"],
    }


def render_diff_layer(sidecar_path: Path, held_out_run_id: str) -> bytes:
    """The one `depth_diff` PNG a compare sidecar has, cached beside its
    source `.tif` (same one-render-per-artifact rule as `rendering.render_and_cache`)."""
    tif_path = sidecar_path.parent / f"{held_out_run_id}__depth_diff.tif"
    return rendering.render_and_cache(tif_path, DIFF_LAYER_ID)


def unavailable_response(site_id: str, scenario_id: str | None) -> dict:
    """Honest `CompareResponse` when no comparison artifact of any kind exists for this site /
    scenario (Feature 11). Replaces the contract example fallback, whose `available: true`,
    all-zero metrics and fictitious run IDs read as a real comparison on the Compare page. No
    metrics, no run IDs, no layers; `unavailable_reason` says what is missing."""
    sid = scenario_id or ""
    if scenario_id:
        sph = _sph_vs_delft3d_unavailable(site_id, scenario_id)
        emu_reason = (f"No emulator LOOCV sidecar for held-out run '{scenario_id}__delft3d' or "
                      f"'{scenario_id}__sph' (written by M5 validation).")
    else:
        sph = {"available": False, "domain": "nearfield", "time_window_s": 0, "metrics": {},
               "probes": [], "layers": [], "run_ids": [],
               "unavailable_reason": "No scenario selected and no default comparison artifact exists for this site."}
        emu_reason = "No scenario selected; an emulator-vs-physics comparison needs a held-out scenario."
    return {
        "site_id": site_id, "scenario_id": sid,
        "sph_vs_delft3d": sph,
        "emulator_vs_physics": {"available": False, "held_out_run_id": None, "metrics": {}, "layers": [],
                                "unavailable_reason": emu_reason},
        "gp_vs_linear": {}, "when_to_use_key": "comparison_unavailable",
        "caveats": [{"id": "comparison_unavailable", "severity": "warning", "text_key": "comparison_unavailable"}],
        "comparison_available": False,
    }

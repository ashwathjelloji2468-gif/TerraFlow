"""M0 ↔ M5 wiring for `POST /flood/query` (Feature 7).

Resolves a flood request against a *persisted* emulator under `data/<site_id>/emulator/<model>/`
(written by `FloodEmulator.save`, normally via `train_site.train_site`), runs the existing
`query.get_flood` in scenario or unknown-breach mode, writes the query artifacts under
`data/<site_id>/queries/<query_id>/` and returns the contract response from
`query.to_contract_response`. No emulator mathematics lives here.

Nothing is fabricated: a missing or incomplete emulator is reported `available: False` with the
missing files, and the empirical fallback (`fallback.py`) is reported with its own availability --
it needs real M1 artifacts *and* a real-terrain loader, which does not exist yet.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from backend.m5_emulator.emulator import OUTPUT_SHORT_NAME, FloodEmulator
from backend.shared.grid import FLOAT_NODATA, CanonicalGrid, write_grid_raster

FALLBACK_REQUIRED = ("hand.tif", "roughness.tif", "chainage_samples.csv", "domain_mask.tif", "grid.json")
EXTENT_LABEL = {1: "POSSIBLE", 2: "HIGH"}


class M5Unavailable(RuntimeError):
    """No usable persisted emulator for the requested site/model."""


class M5InputError(ValueError):
    """The request's inputs cannot be resolved into the emulator's raw inputs."""


def emulator_dir(data_dir: str | Path, site_id: str, model: str) -> Path:
    return Path(data_dir) / site_id / "emulator" / model


def emulator_status(data_dir: str | Path, site_id: str, model: str) -> dict:
    """Is a complete persisted emulator present? Lists every missing file; never loads a GP."""
    root = emulator_dir(data_dir, site_id, model)
    manifest_path = root / "manifest.json"
    out = {"available": False, "model": model, "path": str(root), "missing": []}
    if not manifest_path.is_file():
        out.update(missing=["manifest.json"], reason=f"no trained M5 emulator for {site_id}/{model} at {root}")
        return out
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        out.update(reason=f"unreadable emulator manifest: {e}")
        return out
    if manifest.get("site_id") != site_id or manifest.get("model") != model:
        out.update(reason=f"manifest is for {manifest.get('site_id')}/{manifest.get('model')}, not {site_id}/{model}")
        return out
    shorts = list((manifest.get("outputs") or {}).keys())
    missing = [f for s in shorts for f in (f"pca_{s}.npz", f"gp_{s}.joblib") if not (root / f).is_file()]
    if not shorts:
        out.update(reason="manifest lists no emulated outputs")
        return out
    if missing:
        out.update(missing=missing, reason="emulator files missing: " + ", ".join(missing))
        return out
    validation = root / "validation" / "loocv.json"
    out.update(available=True, reason=None, manifest=str(manifest_path), outputs=shorts,
               validation_report=str(validation) if validation.is_file() else None)
    return out


def fallback_status(data_dir: str | Path, site_id: str) -> dict:
    """The empirical fallback's readiness. `fallback.run_empirical_fallback` needs a
    `FallbackTerrain` built from real M1 rasters; `fallback.py` names that loader
    (`FallbackTerrain.from_rasters`) as future work, so it stays unavailable even when the
    artifacts exist -- `synthetic_fallback_terrain` is test-only and never used here."""
    terrain = Path(data_dir) / site_id / "terrain"
    missing = [f for f in FALLBACK_REQUIRED if not (terrain / f).is_file()]
    if missing:
        reason = "M1 artifacts missing for the empirical fallback: " + ", ".join(missing)
    else:
        reason = ("M1 artifacts present, but no real-terrain loader for the empirical fallback exists "
                  "(fallback.py: FallbackTerrain.from_rasters not implemented)")
    return {"available": False, "missing": missing, "reason": reason}


def resolve_inputs(body: dict, emulator: FloodEmulator, manifest: dict) -> dict[str, float]:
    """Exact request inputs -> raw values keyed by the emulator's input names. Sliders are not
    mapped here (Feature 8). Inputs the emulator does not use are accepted only when they equal
    the manifest's recorded `fixed_inputs` value."""
    names = [s.name for s in emulator.input_scaler.specs]
    fixed = manifest.get("fixed_inputs") or {}
    resolved: dict[str, float] = {}
    for name, item in (body.get("inputs") or {}).items():
        if item.get("type") != "exact":
            raise M5InputError(f"input '{name}': only exact inputs are supported by the M5 query path "
                               "(slider mapping is not implemented)")
        value = float(item["value"])
        if name in names:
            resolved[name] = value
        elif name in fixed:
            if not np.isclose(value, float(fixed[name]), rtol=1e-9, atol=0.0):
                raise M5InputError(f"input '{name}' is fixed at {fixed[name]} in this emulator's training "
                                   f"library; {value} cannot be emulated")
        else:
            raise M5InputError(f"input '{name}' is not an input of this emulator (inputs: {names})")
    if body["mode"] == "scenario":
        missing = [n for n in names if n not in resolved]
        if missing:
            raise M5InputError(f"scenario mode needs an exact value for every emulator input; missing {missing}")
    return resolved


def poi_indices(emulator: FloodEmulator, terrain_dir: Path) -> tuple[dict[str, int], list[str]]:
    """POIs -> flattened grid index (same mapping as `train_site`), keeping only POIs inside the
    trained corridor (`get_flood` requires that). Returns (used, skipped)."""
    from backend.shared.probes import load_probes

    grid = emulator.grid
    used, skipped = {}, []
    corridor = emulator.corridor_mask.reshape(-1)
    for probe in load_probes(terrain_dir):
        row = int(np.floor((grid.origin_y - probe.y_m) / grid.cell_size_m))
        col = int(np.floor((probe.x_m - grid.origin_x) / grid.cell_size_m))
        idx = row * grid.width + col
        if 0 <= row < grid.height and 0 <= col < grid.width and corridor[idx]:
            used[probe.name] = idx
        else:
            skipped.append(probe.name)
    return used, skipped


def validation_skill(report_path: str | None) -> dict[str, str] | None:
    if not report_path:
        return None
    summary = json.loads(Path(report_path).read_text(encoding="utf-8")).get("summary") or {}
    return {k: v["grade"] for k, v in summary.items() if isinstance(v, dict) and "grade" in v}


def _site_has_placeholders(data_dir: Path, site_id: str) -> tuple[bool, str]:
    import warnings

    from backend.shared.site_config import load_site_config, onboarded_config_path

    onboarded = onboarded_config_path(site_id, data_dir)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = load_site_config(site_id, sites_dir=onboarded.parent if onboarded.is_file() else None)
    except Exception as e:  # unknown -> conservatively placeholder
        return True, f"site config unreadable ({e}); treated as placeholder"
    return bool(cfg.placeholder_fields), "site config"


def _write_extent(path: Path, extent_class: np.ndarray, grid: CanonicalGrid, query_id: str) -> None:
    import rasterio.features
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as transform_geometry, unary_union

    to_wgs84 = Transformer.from_crs(grid.crs_epsg, 4326, always_xy=True).transform
    features = []
    for value, label in EXTENT_LABEL.items():
        mask = extent_class == value
        if not mask.any():
            continue
        geoms = [shape(g) for g, v in rasterio.features.shapes(mask.astype(np.uint8), mask=mask,
                                                                 transform=grid.transform) if v == 1]
        union = unary_union(geoms)
        features.append({"type": "Feature", "geometry": mapping(transform_geometry(to_wgs84, union)),
                         "properties": {"extent_class": value, "label": label, "method": "gp_emulator",
                                        "query_id": query_id}})
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")


def run_query(data_dir: str | Path, body: dict, query_id: str) -> dict:
    """Run one M5 query end to end and write its artifacts. Raises `M5Unavailable` /
    `M5InputError` (nothing written) or any computation error (caller marks the query failed)."""
    from backend.m5_emulator.query import get_flood, to_contract_response

    data_dir = Path(data_dir)
    site_id, model, mode = body["site_id"], body["model"], body["mode"]
    status = emulator_status(data_dir, site_id, model)
    if not status["available"]:
        raise M5Unavailable(status["reason"])
    manifest = json.loads(Path(status["manifest"]).read_text(encoding="utf-8"))
    started = time.perf_counter()
    emulator = FloodEmulator.load(emulator_dir(data_dir, site_id, model))
    inputs = resolve_inputs(body, emulator, manifest)
    pois, skipped = poi_indices(emulator, data_dir / site_id / "terrain")
    has_placeholders, placeholder_basis = _site_has_placeholders(data_dir, site_id)
    skill = validation_skill(status.get("validation_report"))
    options = body.get("options") or {}
    kwargs = {}
    if mode == "unknown_breach":
        if "n_samples" in options:
            kwargs["n_samples"] = int(options["n_samples"])
        if "seed" in options:
            kwargs["seed"] = options["seed"]
    result = get_flood(emulator, mode, inputs, pois, validation_skill=skill,
                       has_placeholder_inputs=has_placeholders, **kwargs)
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))

    caveats = []
    if skill is None:
        caveats.append({"id": "emulator_not_validated", "severity": "warning", "text_key": "caveat_emulator_not_validated"})
    if manifest.get("demo_mode"):
        caveats.append({"id": "demo_mode", "severity": "warning", "text_key": "caveat_demo_mode"})
    payload = to_contract_response(result, emulator, query_id=query_id, caveats=caveats,
                                   demo_mode=bool(manifest.get("demo_mode")), has_placeholder_inputs=has_placeholders,
                                   timing_ms={"median_phase": elapsed_ms, "full_phase": elapsed_ms})

    # Persist exactly the arrays M5 returned; drop any listed layer without a written file.
    query_dir = data_dir / site_id / "queries" / query_id
    layers_dir = query_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    grid = emulator.grid
    arrays = {"p_inundation": result.p_inundation.astype(np.float32), "extent_class": result.extent_class.astype(np.uint8)}
    for band, maps in (("p50", result.median), ("p10", result.p10), ("p90", result.p90)):
        for raw_name, array in maps.items():
            arrays[f"{OUTPUT_SHORT_NAME[raw_name]}_{band}"] = np.where(np.isfinite(array), array, FLOAT_NODATA)
    # Only the contract's layer ids (docs/handoff_contract.md layer table, mirrored by M0's style
    # map) are published; M5's velocity_p10 has no contract layer and is not written.
    from backend.m0_api.rendering import LAYER_STYLE_ID

    not_published = sorted(k for k in arrays if k not in LAYER_STYLE_ID)
    for layer_id, array in arrays.items():
        if layer_id in LAYER_STYLE_ID:
            write_grid_raster(layers_dir / f"{layer_id}.tif", array, grid)
    payload["layers"] = [layer for layer in payload["layers"] if (layers_dir / f"{layer['layer_id']}.tif").is_file()]
    _write_extent(query_dir / "extent.geojson", result.extent_class, grid, query_id)

    prov = payload["provenance"]
    prov.update({"emulator_manifest": status["manifest"], "run_ids": list(manifest.get("run_ids") or [])})
    if manifest.get("code_version"):
        prov["code_version"] = manifest["code_version"]
    prov["parameters"].update({
        "site_id": site_id, "model": model, "query_id": query_id, "inputs": inputs,
        "fixed_inputs": manifest.get("fixed_inputs") or {}, "seed": kwargs.get("seed"),
        "trained_at": manifest.get("trained_at"), "validation_report": status.get("validation_report"),
        "validation_skill": skill, "outside_trained_range": result.outside_trained_range,
        "confidence_overall": result.confidence["overall"], "has_placeholders": has_placeholders,
        "placeholder_basis": placeholder_basis, "empirical_fallback_used": False,
        "pois_used": sorted(pois), "pois_outside_corridor": sorted(skipped),
        "demo_mode": bool(manifest.get("demo_mode")), "scenario_id_ignored": body.get("scenario_id"),
        "layers_not_in_contract": not_published,
    })
    payload["flags"]["has_placeholders"] = has_placeholders or payload["flags"]["has_placeholders"]
    (query_dir / "result.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return payload

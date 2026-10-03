"""Feature 16 P4: POI coordinate evidence from a site's OSM exposure extract -- generic, optional.

For every `points_of_interest[i]` of ANY site config, searches the OpenStreetMap extract that
`backend.m6_impact.exposure_osm` writes (`<data>/<site>/exposure/places.gpkg`, `facilities.gpkg`;
docs/data_sources.md src_048) with fixed, deterministic rules (the same rules Feature 15 uses for
Teesta; this module is the site-generic, data-directory variant and does not touch any register):

- village: normalized OSM name == normalized POI name, OSM kind in {city, town, village, hamlet};
- hospital / bridge: OSM kind == category and the OSM name contains every distinctive POI-name
  token (generic words and parenthetical text removed);
- dam (or any other category): UNRESOLVED -- no dam dataset exists in the repository; dam
  coordinates are never guessed.

Exactly one match -> a CANDIDATE with `review_state: pending` and full provenance (dataset path +
SHA-256, layer, OSM feature id/name/kind, match rule, match count, distance from the current
config value). Zero or several matches -> UNRESOLVED with the reason and the matching ids. OSM is
secondary evidence, never authoritative: nothing is approved, nothing is applied, the site config
and every input-resolution register are left untouched.

Output: `<data>/<site>/exposure/poi_evidence.json` (deterministic: no timestamps). A missing or
unreadable extract -> UNVERIFIABLE, and NOTHING is written (an existing evidence file is kept).

CLI: python -m backend.m6_impact.poi_candidates <site_id> [--data-dir DIR] [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

from backend.shared.input_resolution import CANDIDATE, UNRESOLVED
from backend.shared.site_config import SITE_ID_PATTERN, SiteConfig, default_data_dir

UNVERIFIABLE = "UNVERIFIABLE"  # same word as data_foundation / Feature 15 dataset checks
PENDING = "pending"
GENERATOR, GENERATOR_VERSION = "backend.m6_impact.poi_candidates", 1
SOURCE_ID = "src_048"  # OpenStreetMap exposure extract (docs/data_sources.md)
AUTHORITY = "secondary: OpenStreetMap (ODbL), not an authoritative source; reviewer must verify"
OUTPUT_NAME = "poi_evidence.json"
LAYERS = {"places": "places.gpkg", "facilities": "facilities.gpkg"}
SETTLEMENT_CATEGORIES = {"village", "town", "city", "hamlet"}
SETTLEMENT_KINDS = ("city", "hamlet", "town", "village")
FACILITY_CATEGORIES = {"hospital": "hospital", "bridge": "bridge"}
GENERIC_WORDS = {"district", "hospital", "bridge", "road", "the", "of", "and", "govt", "government"}


class UnsafeEvidencePath(ValueError):
    """A dataset or output path would leave `<data_dir>/<site_id>/` (traversal / symlink escape)."""


class ExtractUnavailable(RuntimeError):
    """The OSM extract is not present/readable here: the evidence is UNVERIFIABLE."""


def normalize(name: object) -> str:
    """Case/punctuation/diacritic-insensitive name; non-strings (None, NaN) -> "" (never matches)."""
    if not isinstance(name, str):
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def distinctive_tokens(name: str) -> list[str]:
    return [t for t in normalize(re.sub(r"\([^)]*\)", " ", name)).split() if t not in GENERIC_WORDS]


def haversine_m(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def site_paths(site_id: str, data_dir: Path) -> dict[str, Path]:
    """Every path this module reads/writes, each proven to stay inside `<data_dir>/<site_id>/`."""
    if not isinstance(site_id, str) or not re.fullmatch(SITE_ID_PATTERN, site_id):
        raise UnsafeEvidencePath(f"unsafe or invalid site id {site_id!r}")
    site_root = (Path(data_dir) / site_id).resolve()
    exposure = Path(data_dir) / site_id / "exposure"
    paths = {k: exposure / v for k, v in LAYERS.items()} | {"output": exposure / OUTPUT_NAME}
    for key, p in paths.items():
        if not p.resolve().is_relative_to(site_root):
            raise UnsafeEvidencePath(f"{key} path resolves outside data/{site_id}/ (symlink escape)")
    return paths


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path) -> list[dict]:
    import geopandas as gpd
    gdf = gpd.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    out = []
    for _, r in gdf.iterrows():
        if r.geometry is None or r.geometry.is_empty:
            continue
        c = r.geometry.centroid
        out.append({"osm_id": str(r["osm_id"]), "kind": r["kind"] if isinstance(r["kind"], str) else "",
                    "name": r["name"] if isinstance(r["name"], str) else "",
                    "lonlat": [round(float(c.x), 6), round(float(c.y), 6)]})
    return sorted(out, key=lambda r: (r["osm_id"], r["lonlat"]))


def _match(poi, places, facilities):
    category = poi.category
    if category in SETTLEMENT_CATEGORIES:
        want = normalize(poi.name)
        rows = [r for r in places if r["kind"] in SETTLEMENT_KINDS and normalize(r["name"]) == want]
        return "places", rows, f"places.gpkg kind in {list(SETTLEMENT_KINDS)} and normalized name == {want!r}"
    if category in FACILITY_CATEGORIES:
        kind, tokens = FACILITY_CATEGORIES[category], distinctive_tokens(poi.name)
        if not tokens:
            return "facilities", [], f"no distinctive tokens in POI name {poi.name!r}"
        rows = [r for r in facilities if r["kind"] == kind and all(t in normalize(r["name"]).split() for t in tokens)]
        return "facilities", rows, f"facilities.gpkg kind == {kind!r} and name contains all of {tokens}"
    return None, [], f"no dataset in the repository for category {category!r}"


def extract(cfg: SiteConfig, *, data_dir: str | Path | None = None) -> dict:
    """Deterministic evidence for `cfg`'s POIs. Raises ExtractUnavailable (-> UNVERIFIABLE)."""
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    site_id = cfg.site.id
    paths = site_paths(site_id, data_dir)
    missing = [f"exposure/{LAYERS[k]}" for k in LAYERS if not paths[k].is_file()]
    if missing:
        raise ExtractUnavailable(f"{UNVERIFIABLE}: OSM extract not present for '{site_id}': {', '.join(missing)}")
    try:
        layers = {k: _rows(paths[k]) for k in LAYERS}
    except Exception as e:  # noqa: BLE001 -- an unreadable extract is unverifiable, never "no match"
        raise ExtractUnavailable(f"{UNVERIFIABLE}: OSM extract unreadable for '{site_id}' ({type(e).__name__})") from None
    datasets = {k: {"path": f"exposure/{LAYERS[k]}", "sha256": _sha256(paths[k])} for k in LAYERS}
    candidates, unresolved = [], []
    for i, poi in enumerate(cfg.points_of_interest):
        base = {"target": f"points_of_interest[{i}].location", "poi_id": poi.id, "poi_name": poi.name,
                "category": poi.category}
        layer, matches, rule = _match(poi, layers["places"], layers["facilities"])
        if layer is None or len(matches) != 1:
            reason = (f"dam / unsupported category: {rule}" if layer is None
                      else "no match" if not matches else f"ambiguous: {len(matches)} matches")
            unresolved.append({**base, "status": UNRESOLVED, "reason": reason, "match_rule": rule,
                               "match_count": len(matches), "matching_feature_ids": [m["osm_id"] for m in matches]})
            continue
        m, current = matches[0], poi.location.value
        candidates.append({
            **base, "status": CANDIDATE, "review_state": PENDING, "proposed_value": m["lonlat"], "unit": "deg",
            "source_id": SOURCE_ID, "basis": "direct" if layer == "places" else "secondary_dataset",
            "current_value_status": poi.location.status,
            "dataset": {**datasets[layer], "layer": layer, "feature_id": m["osm_id"], "feature_name": m["name"],
                        "feature_kind": m["kind"], "match_rule": rule, "match_count": 1,
                        "distance_from_current_value_m": round(haversine_m(current, m["lonlat"]), 1) if current else None},
        })
    return {"site_id": site_id, "generator": GENERATOR, "generator_version": GENERATOR_VERSION,
            "source_id": SOURCE_ID, "authority": AUTHORITY, "status": "COMPLETE",
            "note": "Evidence only. CANDIDATE = one OSM match awaiting a named reviewer; never approved or applied here.",
            "datasets": datasets, "counts": {CANDIDATE: len(candidates), UNRESOLVED: len(unresolved)},
            "candidates": candidates, "unresolved": unresolved}


def write(result: dict, *, data_dir: str | Path | None = None) -> tuple[Path, bool]:
    """Write `exposure/poi_evidence.json` atomically; (path, changed). Identical content is not rewritten."""
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    out = site_paths(result["site_id"], data_dir)["output"]
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if out.is_file() and out.read_text(encoding="utf-8") == text:
        return out, False
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return out, True


def run(cfg: SiteConfig, *, data_dir: str | Path | None = None, write_output: bool = True) -> dict:
    """Extract (+ write). Never raises for a missing extract: returns status UNVERIFIABLE, writes nothing."""
    try:
        result = extract(cfg, data_dir=data_dir)
    except ExtractUnavailable as e:
        return {"site_id": cfg.site.id, "status": UNVERIFIABLE, "reason": str(e), "written": False}
    out = {"site_id": cfg.site.id, "status": result["status"], "counts": result["counts"], "written": False}
    if write_output:
        _, out["written"] = write(result, data_dir=data_dir)
    return out | {"result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="POI evidence from the site's OSM exposure extract (optional)")
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--write", action="store_true", help="write exposure/poi_evidence.json (default: dry run)")
    args = parser.parse_args(argv)
    from backend.m3_dflowfm.preflight import _load_config
    data_dir = args.data_dir or default_data_dir()
    try:
        site_paths(args.site_id, data_dir)
        cfg, _ = _load_config(args.site_id, Path(data_dir), None)
    except Exception as e:  # noqa: BLE001
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    out = run(cfg, data_dir=data_dir, write_output=args.write)
    out.pop("result", None)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 2 if out["status"] == UNVERIFIABLE else 0


if __name__ == "__main__":
    sys.exit(main())

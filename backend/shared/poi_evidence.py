"""Feature 15: POI coordinate evidence from the site's OpenStreetMap exposure extract (src_048).

Read-only extractor. For every `points_of_interest[i]` in `sites/<site>.yaml` it searches the OSM
extract written by `backend.m6_impact.exposure_osm` (`data/<site>/exposure/places.gpkg` for
settlements, `facilities.gpkg` for hospitals and bridges) with a fixed, deterministic name rule:

- settlements (category village/town/city): normalized OSM name == normalized POI name, OSM kind in
  {city, town, village, hamlet}. The config itself names "OSM settlement centroid" as an acceptable
  source for these, so the basis is `direct`.
- hospitals / bridges: OSM kind matches the category and the OSM name contains every distinctive
  token of the POI name (generic words and parenthetical text removed). The config names other
  authorities for these (health-department list, PWD/BRO/NHAI, imagery), so the basis is
  `secondary_dataset`.
- dams and any other category: UNRESOLVED (no dataset in the repository; CWC register needed).

Exactly one match -> a PENDING candidate with the dataset path + SHA-256, layer, OSM feature ID,
OSM name/kind, the match rule, match count (1) and the distance from the current placeholder.
Zero or several matches -> UNRESOLVED with the reason and the matching OSM IDs. Nothing is ever
approved here, and `sites/<site>.yaml` is never touched.

Output: `config/input_resolution/<site>.poi_candidates.yaml` (generated; merged by
`input_resolution.load_register`). Re-running keeps any entry a reviewer already decided.

CLI: python -m backend.shared.poi_evidence <site_id> [--data-dir DIR] [--write-candidates]
     (without --write-candidates: dry run, prints the result). Missing extract -> UNVERIFIABLE, exit 2,
     nothing written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backend.shared import input_resolution as ir

SOURCE_ID = "src_048"
SETTLEMENT_CATEGORIES = {"village", "town", "city", "hamlet"}
SETTLEMENT_KINDS = {"city", "town", "village", "hamlet"}
FACILITY_CATEGORIES = {"hospital": "hospital", "bridge": "bridge"}
GENERIC_WORDS = {"district", "hospital", "bridge", "road", "the", "of", "and", "govt", "government"}
OUTPUT_SUFFIX = "poi_candidates"


class ExtractUnavailable(RuntimeError):
    """The OSM extract is not on this machine: the evidence is UNVERIFIABLE here."""


def normalize(name: object) -> str:
    """Case/punctuation/diacritic-insensitive form of a name. Anything that is not a non-empty
    string (None, NaN from a NULL GeoPackage cell, numbers, ...) normalizes to "" -- it is never
    coerced into a matchable name, and "" can never match a POI (POI names are non-empty)."""
    if not isinstance(name, str):
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def distinctive_tokens(name: str) -> list[str]:
    without_parens = re.sub(r"\([^)]*\)", " ", name)
    return [t for t in normalize(without_parens).split() if t not in GENERIC_WORDS]


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_layer(path: Path):
    import geopandas as gpd
    gdf = gpd.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


def _match(poi: dict, places, facilities) -> tuple[str | None, list[dict], str]:
    """(layer, matching rows, rule) for one POI; layer None = category not searchable."""
    category = (poi.get("category") or "").lower()
    if category in SETTLEMENT_CATEGORIES:
        want = normalize(poi["name"])
        rows = [r for r in places if r["kind"] in SETTLEMENT_KINDS and normalize(r["name"]) == want]
        return "places", rows, f"places.gpkg kind in {sorted(SETTLEMENT_KINDS)} and normalized name == {want!r}"
    if category in FACILITY_CATEGORIES:
        kind, tokens = FACILITY_CATEGORIES[category], distinctive_tokens(poi["name"])
        if not tokens:
            return "facilities", [], f"no distinctive tokens in POI name {poi['name']!r}"
        rows = [r for r in facilities if r["kind"] == kind and all(t in normalize(r["name"]).split() for t in tokens)]
        return "facilities", rows, f"facilities.gpkg kind == {kind!r} and name contains all of {tokens}"
    return None, [], f"no dataset in the repository for category {category!r}"


def extract(site_id: str, *, data_dir: Path | None = None, sites_dir: Path = ir.SITES_DIR) -> dict:
    from backend.shared.site_config import default_data_dir
    from backend.shared.version import version_info
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    exposure = data_dir / site_id / "exposure"
    layers = {"places": exposure / "places.gpkg", "facilities": exposure / "facilities.gpkg"}
    missing = [str(p.relative_to(data_dir)) for p in layers.values() if not p.is_file()]
    if missing:
        raise ExtractUnavailable("UNVERIFIABLE: OSM extract not present on this machine: " + ", ".join(missing))
    hashes = {k: _sha256(p) for k, p in layers.items()}

    def rows(key):
        gdf = _read_layer(layers[key])
        return [{"osm_id": str(r["osm_id"]), "kind": r["kind"],
                 "name": r["name"] if isinstance(r["name"], str) else "",  # NULL/NaN name -> "" (never matches)
                 "lonlat": (round(float(r.geometry.centroid.x), 6), round(float(r.geometry.centroid.y), 6))}
                for _, r in gdf.iterrows() if r.geometry is not None and not r.geometry.is_empty]
    places, facilities = rows("places"), rows("facilities")
    site = yaml.safe_load((sites_dir / f"{site_id}.yaml").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries, unresolved = [], []
    for i, poi in enumerate(site.get("points_of_interest") or []):
        target = f"points_of_interest[{i}].location"
        placeholder = tuple(poi["location"]["value"]) if poi["location"].get("value") else None
        layer, matches, rule = _match(poi, places, facilities)
        base = {"target": target, "poi_id": poi.get("id"), "poi_name": poi.get("name"), "category": poi.get("category")}
        if layer is None or len(matches) != 1:
            reason = ("dam / unsupported category: " + rule) if layer is None else \
                     ("no match" if not matches else f"ambiguous: {len(matches)} matches")
            unresolved.append({**base, "reason": f"UNRESOLVED ({reason})", "match_rule": rule, "match_count": len(matches),
                               "matching_feature_ids": [m["osm_id"] for m in matches]})
            continue
        m = matches[0]
        settlement = layer == "places"
        entries.append({
            **base, "proposed_value": [m["lonlat"][0], m["lonlat"][1]], "unit": "deg", "source_id": SOURCE_ID,
            "basis": "direct" if settlement else "secondary_dataset",
            "notes": ("Config names 'OSM settlement centroid' as an acceptable source for this POI." if settlement else
                      "OSM is secondary here: the config names another authority; reviewer must check the match on imagery."),
            "dataset": {"path": str(layers[layer].relative_to(data_dir)), "sha256": hashes[layer], "layer": layer,
                        "feature_id": m["osm_id"], "feature_name": m["name"], "feature_kind": m["kind"],
                        "match_rule": rule, "match_count": 1,
                        "distance_from_placeholder_m": round(haversine_m(placeholder, m["lonlat"]), 1) if placeholder else None},
            "decision": {"state": "pending", "reviewer": None, "decided_at": None, "rationale": None},
        })
    return {"site_id": site_id, "generated_at": now, "generator": "backend.shared.poi_evidence",
            **{k: v for k, v in version_info().items() if k != "contract_version"},
            "datasets": {k: {"path": str(p.relative_to(data_dir)), "sha256": hashes[k]} for k, p in layers.items()},
            "entries": entries, "unresolved": unresolved}


def write_candidates(result: dict, register_dir: Path = ir.REGISTER_DIR) -> Path:
    """Write the generated companion file, keeping any entry a reviewer already decided."""
    path = register_dir / f"{result['site_id']}.{OUTPUT_SUFFIX}.yaml"
    decided = {}
    if path.is_file():
        old = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        decided = {e["target"]: e for e in old.get("entries") or [] if ir.decision_of(e)["state"] != "pending"}
    entries = [decided.get(e["target"], e) for e in result["entries"]]
    kept = [e for t, e in decided.items() if t not in {x["target"] for x in entries}]
    body = {**{k: v for k, v in result.items() if k not in ("entries", "unresolved")},
            "entries": entries + kept,
            "unresolved": [u for u in result["unresolved"] if u["target"] not in decided]}
    header = ("# GENERATED by `python -m backend.shared.poi_evidence` (Feature 15). Reviewers edit only the\n"
              "# `decision` blocks; re-running keeps decided entries. Candidates are PENDING until reviewed.\n")
    path.write_text(header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="POI coordinate evidence from the OSM exposure extract")
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--write-candidates", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = extract(args.site_id, data_dir=args.data_dir)
    except ExtractUnavailable as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.write_candidates:
        print(f"wrote {write_candidates(result)}")
    print(json.dumps({"candidates": len(result["entries"]), "unresolved": len(result["unresolved"]),
                      "entries": [{k: e[k] for k in ("target", "poi_name", "proposed_value", "basis")} for e in result["entries"]],
                      "unresolved_detail": [{k: u[k] for k in ("target", "poi_name", "reason")} for u in result["unresolved"]]},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

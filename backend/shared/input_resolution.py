"""Feature 14: evidence-tracked resolution of a site's placeholder inputs.

A register (`config/input_resolution/<site_id>.yaml`) holds, per site-config input, a PROPOSED
value, the `docs/data_sources.md` source ID, a verbatim quote and the basis (direct / mapping /
interpretation). Tooling never decides anything. Feature 15: each entry carries a reviewer
`decision` -- `{state: pending|approved|rejected, reviewer, decided_at, rationale}`; approved and
rejected decisions need all three of reviewer, date and rationale. Only APPROVED entries can be
applied; REJECTED entries are kept as a record and never applied. The Feature 14 `approval`
block (`approved_by` + `approved_at`) is still read, but an entry may not carry both forms.

Commands (`python -m backend.shared.input_resolution ...`):
  validate <site>            check every entry against the site config and data_sources.md
  report <site> [--out MD]   per production-gate input: APPROVED / CANDIDATE / UNRESOLVED (+ required artifact)
  apply <site> [--write]     copy APPROVED entries into sites/<site>.yaml (status -> sourced); dry run by default

Statuses: APPROVED (approved entry), CANDIDATE (documented evidence, awaiting review / pending),
REJECTED (a reviewer rejected it, with rationale), UNRESOLVED
(no repository source; the config's own "Replace from:" text names the required artifact).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER_DIR = REPO_ROOT / "config" / "input_resolution"
DATA_SOURCES = REPO_ROOT / "docs" / "data_sources.md"
SITES_DIR = REPO_ROOT / "sites"
APPROVED, CANDIDATE, REJECTED, UNRESOLVED = "APPROVED", "CANDIDATE", "REJECTED", "UNRESOLVED"
DECISION_STATES = ("pending", "approved", "rejected")
BASES = ("direct", "mapping", "interpretation", "secondary_dataset")
#: Feature 15: dataset-evidence check results (an entry with a `dataset` block instead of a quote).
DATASET_VERIFIED, DATASET_UNVERIFIABLE, DATASET_MISMATCH = "VERIFIED", "UNVERIFIABLE", "MISMATCH"


class RegisterError(ValueError):
    pass


def _norm(text: str) -> str:
    return " ".join(text.split())


def source_sections(path: Path = DATA_SOURCES) -> dict[str, str]:
    """`{src_id: normalized section text}` from docs/data_sources.md (## src_NNN headings)."""
    sections, current, buf = {}, None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(src_\d+)\b", line)
        if m:
            if current:
                sections[current] = _norm("\n".join(buf))
            current, buf = m.group(1), [line]
        elif current:
            buf.append(line)
    if current:
        sections[current] = _norm("\n".join(buf))
    return sections


def load_register(site_id: str, register_dir: Path = REGISTER_DIR) -> dict:
    """The site's hand-written register `<site>.yaml` merged with generated companion files
    `<site>.*.yaml` (Feature 15, e.g. `teesta.poi_candidates.yaml` from `poi_evidence`)."""
    reg = {"site_id": site_id, "entries": [], "evidence_only": [], "unresolved": []}
    paths = [register_dir / f"{site_id}.yaml", *sorted(register_dir.glob(f"{site_id}.*.yaml"))]
    for path in paths:
        if not path.is_file():
            continue
        part = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if part.get("site_id") not in (None, site_id):
            reg["site_id"] = part.get("site_id")
        for key in ("entries", "evidence_only", "unresolved"):
            for item in part.get(key) or []:
                reg[key].append({**item, "_file": path.name})
    return reg


def dataset_check(entry: dict, data_dir: Path | None = None) -> str | None:
    """`None` for quote-evidence entries; else VERIFIED / UNVERIFIABLE (file not on this machine) /
    MISMATCH (file present but its SHA-256 differs from the recorded one)."""
    ds = entry.get("dataset")
    if not ds:
        return None
    import hashlib
    from backend.shared.site_config import default_data_dir
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    path = root / ds["path"]
    if not path.is_file():
        return DATASET_UNVERIFIABLE
    return DATASET_VERIFIED if hashlib.sha256(path.read_bytes()).hexdigest() == ds.get("sha256") else DATASET_MISMATCH


def _sourced_values(site_yaml: dict) -> dict[str, dict]:
    out = {}
    def walk(node, path):
        if isinstance(node, dict):
            if {"value", "unit", "source", "status"} <= set(node):
                out[path] = node
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(site_yaml, "")
    return out


def decision_of(entry: dict) -> dict:
    """Normalized decision `{state, reviewer, decided_at, rationale}` (Feature 15), reading the
    Feature 14 `approval` block as a legacy approval."""
    d = entry.get("decision")
    if d is not None:
        return {"state": (d or {}).get("state") or "pending", "reviewer": (d or {}).get("reviewer"),
                "decided_at": (d or {}).get("decided_at"), "rationale": (d or {}).get("rationale")}
    a = entry.get("approval") or {}
    if a.get("approved_by") and a.get("approved_at"):
        return {"state": "approved", "reviewer": a["approved_by"], "decided_at": a["approved_at"], "rationale": None}
    return {"state": "pending", "reviewer": None, "decided_at": None, "rationale": None}


def decision_problems(entry: dict) -> list[str]:
    problems = []
    if entry.get("decision") is not None and any((entry.get("approval") or {}).values()):
        problems.append("carries both `decision` and a filled legacy `approval`; keep one")
    a = entry.get("approval") or {}
    if entry.get("decision") is None and bool(a.get("approved_by")) != bool(a.get("approved_at")):
        problems.append("approval needs both approved_by and approved_at")
    if entry.get("decision") is not None:
        d = decision_of(entry)
        if d["state"] not in DECISION_STATES:
            problems.append(f"decision.state must be one of {DECISION_STATES}")
        elif d["state"] in ("approved", "rejected"):
            missing = [k for k in ("reviewer", "decided_at", "rationale") if not d[k]]
            if missing:
                problems.append(f"{d['state']} decision needs reviewer, decided_at and rationale (missing: {', '.join(missing)})")
        elif any(d[k] for k in ("reviewer", "decided_at")):
            problems.append("pending decision must not carry a reviewer or date")
    return problems


def entry_status(entry: dict) -> str:
    state = decision_of(entry)["state"]
    if decision_problems(entry):
        return CANDIDATE  # an incomplete/invalid decision is never treated as decided
    return {"approved": APPROVED, "rejected": REJECTED}.get(state, CANDIDATE)


def validate(site_id: str, *, sites_dir: Path = SITES_DIR, register_dir: Path = REGISTER_DIR,
             data_sources: Path = DATA_SOURCES, data_dir: Path | None = None) -> list[str]:
    """All problems found (empty list = valid). Every quote must be verbatim in its cited section."""
    reg = load_register(site_id, register_dir)
    site = yaml.safe_load((sites_dir / f"{site_id}.yaml").read_text(encoding="utf-8"))
    values = _sourced_values(site)
    sections = source_sections(data_sources)
    problems, seen = [], set()
    if reg.get("site_id") not in (None, site_id):
        problems.append(f"register site_id {reg.get('site_id')!r} != {site_id!r}")
    for i, e in enumerate(reg["entries"]):
        where = f"entries[{i}] ({e.get('target')})"
        target = e.get("target")
        if target in seen:
            problems.append(f"{where}: duplicate target")
        seen.add(target)
        if target not in values:
            problems.append(f"{where}: target is not a sourced value in sites/{site_id}.yaml")
        elif e.get("unit") != values[target].get("unit"):
            problems.append(f"{where}: unit {e.get('unit')!r} != config unit {values[target].get('unit')!r}")
        if e.get("proposed_value") is None:
            problems.append(f"{where}: proposed_value is required")
        if e.get("basis") not in BASES:
            problems.append(f"{where}: basis must be one of {BASES}")
        if e.get("dataset"):
            problems += _dataset_problems(where, e, sections, data_dir)
        else:
            problems += _quote_problems(where, e, sections)
        problems += [f"{where}: {p}" for p in decision_problems(e)]
    for i, e in enumerate(reg["evidence_only"]):
        where = f"evidence_only[{i}] ({e.get('target')})"
        if e.get("target") not in values:
            problems.append(f"{where}: target is not a sourced value in sites/{site_id}.yaml")
        problems += _quote_problems(where, e, sections)
    return problems


DATASET_FIELDS = ("path", "sha256", "layer", "feature_id", "match_rule", "match_count", "distance_from_placeholder_m")


def _dataset_problems(where: str, e: dict, sections: dict[str, str], data_dir: Path | None) -> list[str]:
    out = []
    if e.get("source_id") not in sections:
        out.append(f"{where}: source_id {e.get('source_id')!r} not found in docs/data_sources.md")
    missing = [k for k in DATASET_FIELDS if e["dataset"].get(k) in (None, "")]
    if missing:
        out.append(f"{where}: dataset evidence missing {', '.join(missing)}")
    if e["dataset"].get("match_count") != 1:
        out.append(f"{where}: dataset candidate must come from exactly one match")
    if dataset_check(e, data_dir) == DATASET_MISMATCH:
        out.append(f"{where}: dataset sha256 mismatch for {e['dataset'].get('path')}")
    return out


def _quote_problems(where: str, e: dict, sections: dict[str, str]) -> list[str]:
    sid, quote = e.get("source_id"), e.get("quote")
    if sid not in sections:
        return [f"{where}: source_id {sid!r} not found in docs/data_sources.md"]
    if not quote or _norm(quote) not in sections[sid]:
        return [f"{where}: quote not found verbatim in {sid}"]
    return []


def report(site_id: str, *, data_dir: Path | None = None, sites_dir: Path = SITES_DIR,
           register_dir: Path = REGISTER_DIR, checklist_path: Path | None = None) -> dict:
    """Join the production gate's blocking site-config items with the register."""
    from backend.m3_dflowfm import production_gate
    kwargs = {"sites_dir": sites_dir}
    if data_dir is not None:
        kwargs["data_dir"] = data_dir
    if checklist_path is not None:
        kwargs["checklist_path"] = checklist_path
    gate = production_gate.evaluate(site_id, **kwargs)
    reg = load_register(site_id, register_dir)
    entries = {e["target"]: e for e in reg["entries"]}
    evidence = {}
    for e in reg["evidence_only"]:
        evidence.setdefault(e["target"], []).append(e)
    unresolved = {u["target"]: u for u in reg.get("unresolved", [])}
    site = yaml.safe_load((sites_dir / f"{site_id}.yaml").read_text(encoding="utf-8"))
    values = _sourced_values(site)
    rows = []
    for item in gate["items"]:
        if item["category"] != "site_config" or not item["blocking"] or item["status"] == production_gate.READY:
            continue
        key, e = item["key"], entries.get(item["key"])
        src_text = (values.get(key) or {}).get("source") or ""
        replace = src_text.split("Replace from:", 1)[1].strip() if "Replace from:" in src_text else None
        rows.append({"key": key, "gate_status": item["status"],
                     "resolution": entry_status(e) if e else UNRESOLVED,
                     "proposed_value": e.get("proposed_value") if e else None,
                     "source_id": e.get("source_id") if e else None,
                     "basis": e.get("basis") if e else None,
                     "decision": decision_of(e) if e else None,
                     "evidence_only": [{"source_id": x["source_id"], "reason": x["reason"]} for x in evidence.get(key, [])],
                     "dataset_check": dataset_check(e, data_dir) if e else None,
                     "unresolved_reason": (unresolved.get(key) or {}).get("reason") if not e else None,
                     "required_artifact": replace})
    counts = {s: sum(1 for r in rows if r["resolution"] == s) for s in (APPROVED, CANDIDATE, REJECTED, UNRESOLVED)}
    return {"site_id": site_id, "gate_verdict": gate["verdict"], "evaluated_at": gate["evaluated_at"],
            "counts": counts, "rows": rows,
            "non_config_blocking": [i["key"] for i in gate["items"]
                                    if i["blocking"] and i["status"] != production_gate.READY and i["category"] != "site_config"]}


def report_markdown(rep: dict) -> str:
    lines = [f"# {rep['site_id']} input resolution", "",
             "Generated by `python -m backend.shared.input_resolution report "
             f"{rep['site_id']} --out ...`. Do not edit by hand.", "",
             f"Production gate: **{rep['gate_verdict']}** · APPROVED {rep['counts'][APPROVED]} · "
             f"CANDIDATE {rep['counts'][CANDIDATE]} · REJECTED {rep['counts'][REJECTED]} · "
             f"UNRESOLVED {rep['counts'][UNRESOLVED]}", "",
             "A CANDIDATE is documented evidence awaiting a named reviewer; it is not a production input.", "",
             "| Input | Gate | Resolution | Proposed | Source | Basis | Required artifact / note |",
             "|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        note = r["required_artifact"] or ""
        if r["resolution"] == REJECTED and r.get("decision"):
            d = r["decision"]
            note = f"rejected by {d['reviewer']} on {d['decided_at']}: {d['rationale']}" + (f" — {note}" if note else "")
        if r.get("dataset_check"):
            note = f"dataset {r['dataset_check']}" + (f" — {note}" if note else "")
        if r.get("unresolved_reason"):
            note = f"{r['unresolved_reason']}" + (f" — {note}" if note else "")
        if r["evidence_only"]:
            note = "; ".join(f"{x['source_id']}: {x['reason']}" for x in r["evidence_only"]) + (f" — {note}" if note else "")
        lines.append(f"| `{r['key']}` | {r['gate_status']} | {r['resolution']} | "
                     f"{'' if r['proposed_value'] is None else r['proposed_value']} | {r['source_id'] or ''} | "
                     f"{r['basis'] or ''} | {note.replace('|', '/')} |")
    lines += ["", "Other blocking gate items (checklist / shared gates / artifacts): "
              + (", ".join(f"`{k}`" for k in rep["non_config_blocking"]) or "none"), ""]
    return "\n".join(lines)


_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*)$")


def _locate_block(lines: list[str], target: str) -> tuple[int, int]:
    """(start, end) line indices of the mapping at `target` (e.g. `dams[1].breach_inputs.dam_height`)
    in a block-style YAML file, so it can be edited without losing comments."""
    parts = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", target)
    tokens = [p[0] if p[0] else int(p[1]) for p in parts]
    start, end, indent = 0, len(lines), -1
    for tok in tokens:
        found = None
        if isinstance(tok, str):
            for i in range(start, end):
                line = lines[i]
                stripped = line.lstrip(" ")
                ind = len(line) - len(stripped)
                m = _LIST_ITEM.match(line)
                if m and len(m.group(1)) > indent and m.group(2).startswith(f"{tok}:"):
                    found, ind = i, len(m.group(1)) + 2
                elif ind > indent and stripped.startswith(f"{tok}:") and not stripped.startswith("#"):
                    found = i
                if found is not None:
                    indent = ind
                    break
        else:
            count = -1
            for i in range(start, end):
                m = _LIST_ITEM.match(lines[i])
                if m and len(m.group(1)) >= indent and not m.group(2).startswith("#"):
                    if indent < 0 or len(m.group(1)) == min_list_indent(lines, start, end):
                        count += 1
                        if count == tok:
                            found, indent = i, len(m.group(1)) + 1
                            break
        if found is None:
            raise RegisterError(f"cannot locate {target!r} in site YAML")
        start = found + (0 if isinstance(tok, int) else 1)
        end = _block_end(lines, found, indent if isinstance(tok, str) else indent - 1)
        if isinstance(tok, int):
            indent = indent  # list item content indent already set
    return start, end


def min_list_indent(lines, start, end) -> int:
    return min(len(m.group(1)) for i in range(start, end) if (m := _LIST_ITEM.match(lines[i])))


def _block_end(lines: list[str], header: int, indent: int) -> int:
    for i in range(header + 1, len(lines)):
        s = lines[i]
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        ind = len(s) - len(s.lstrip(" "))
        if ind <= indent and not (ind == indent and _LIST_ITEM.match(s) and False):
            return i
    return len(lines)


def _set_field(lines: list[str], start: int, end: int, field: str, value_text: str) -> None:
    for i in range(start, end):
        stripped = lines[i].lstrip(" ")
        if stripped.startswith(f"{field}:"):
            ind = len(lines[i]) - len(stripped)
            lines[i] = " " * ind + f"{field}: {value_text}"
            return
    raise RegisterError(f"field {field!r} not found in block")


def apply(site_id: str, *, write: bool = False, sites_dir: Path = SITES_DIR, register_dir: Path = REGISTER_DIR,
          data_sources: Path = DATA_SOURCES, data_dir: Path | None = None) -> dict:
    """Copy APPROVED entries into sites/<site>.yaml (value, status: sourced, source citation).
    Refuses if the register is invalid. CANDIDATE entries are never applied."""
    problems = validate(site_id, sites_dir=sites_dir, register_dir=register_dir, data_sources=data_sources,
                        data_dir=data_dir)
    if problems:
        raise RegisterError("register invalid: " + "; ".join(problems))
    path = sites_dir / f"{site_id}.yaml"
    lines = path.read_text(encoding="utf-8").splitlines()
    applied, skipped = [], []
    for e in load_register(site_id, register_dir)["entries"]:
        status = entry_status(e)
        if status != APPROVED:
            skipped.append({"target": e["target"], "reason": "rejected by reviewer" if status == REJECTED
                            else "not approved (CANDIDATE)"})
            continue
        check = dataset_check(e, data_dir)
        if check is not None and check != DATASET_VERIFIED:  # dataset evidence must be re-verifiable here
            skipped.append({"target": e["target"], "reason": f"dataset {check}: cannot verify on this machine"})
            continue
        d = decision_of(e)
        a = {"approved_by": d["reviewer"], "approved_at": d["decided_at"]}
        start, end = _locate_block(lines, e["target"])
        evidence = (f"\"{e['quote']}\"" if not e.get("dataset") else
                    f"{e['dataset']['path']} {e['dataset']['feature_id']} (sha256 {e['dataset']['sha256'][:12]})")
        citation = (f"{e['source_id']} (docs/data_sources.md): {evidence} [{e['basis']}]; "
                    f"approved by {a['approved_by']} on {a['approved_at']} (config/input_resolution/{e.get('_file', site_id + '.yaml')})")
        _set_field(lines, start, end, "value", json.dumps(e["proposed_value"], ensure_ascii=False))
        _set_field(lines, start, end, "source", json.dumps(citation, ensure_ascii=False))
        _set_field(lines, start, end, "status", "sourced")
        applied.append(e["target"])
    text = "\n".join(lines) + "\n"
    if applied:
        from backend.shared.site_config import load_site_config
        tmp_dir = path.parent / f".{site_id}.apply"
        tmp_dir.mkdir(exist_ok=True)
        try:
            (tmp_dir / f"{site_id}.yaml").write_text(text, encoding="utf-8")
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                load_site_config(site_id, sites_dir=tmp_dir)  # result must still be a valid site config
        finally:
            for p in tmp_dir.iterdir():
                p.unlink()
            tmp_dir.rmdir()
        if write:
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
    return {"site_id": site_id, "written": bool(write and applied), "applied": applied, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evidence-tracked site input resolution (Feature 14)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "report", "apply"):
        p = sub.add_parser(name)
        p.add_argument("site_id")
        if name == "report":
            p.add_argument("--out", type=Path)
            p.add_argument("--json", action="store_true")
        if name == "apply":
            p.add_argument("--write", action="store_true", help="write sites/<site>.yaml (default: dry run)")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "validate":
            problems = validate(args.site_id)
            print("\n".join(problems) if problems else "register valid")
            return 1 if problems else 0
        if args.cmd == "report":
            rep = report(args.site_id)
            text = json.dumps(rep, indent=2) if args.json else report_markdown(rep)
            if args.out:
                args.out.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
            print(text)
            return 0
        out = apply(args.site_id, write=args.write)
        print(json.dumps(out, indent=2))
        return 0
    except RegisterError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

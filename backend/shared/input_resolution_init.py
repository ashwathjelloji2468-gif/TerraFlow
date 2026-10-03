"""Feature 16 P3: create an EMPTY input-resolution register for any onboarded/bundled site.

Uses the existing Feature 14 register format (`backend.shared.input_resolution`) unchanged:
`{site_id, entries: [], evidence_only: []}`. No entry is ever generated: an entry needs a proposed
value, a source id and a verbatim quote, and this tool has none of them. With no entry, every input
the production gate lists as blocking is reported UNRESOLVED by `input_resolution report` -- the
existing semantics. The header comment lists those inputs (from `production_gate.config_items`,
a pure function of the site config) so a reviewer sees what is pending; the list is informational,
the report remains the source of truth.

Safety:
- the site id must match the contract site-id pattern (`site_config.SITE_ID_PATTERN`);
- the site config must load (bundled `sites/<id>.yaml` or onboarded `<data>/<id>/config/<id>.yaml`)
  and its `site.id` must equal the requested id;
- the register path is `<register_dir>/<id>.yaml` and must resolve inside `register_dir`;
- the file is created with exclusive create (`open(..., "x")`): an existing register (or anything
  at that path, including a symlink) is never read-modified-written or replaced -> explicit no-op.

Deterministic: same config + same tool -> byte-identical file (no timestamps, ids or absolute paths).

CLI: python -m backend.shared.input_resolution_init <site_id> [--data-dir DIR] [--sites-dir DIR]
     [--register-dir DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from backend.shared import input_resolution as ir
from backend.shared.site_config import SITE_ID_PATTERN, default_data_dir

GENERATOR = "backend.shared.input_resolution_init"
GENERATOR_VERSION = 1
CREATED, EXISTS, DRY_RUN = "created", "exists", "dry_run"


class InitError(ValueError):
    """The register cannot be initialised (unsafe id, unknown site, unsafe path)."""


def _check_site_id(site_id: object) -> str:
    if not isinstance(site_id, str) or not re.fullmatch(SITE_ID_PATTERN, site_id):
        raise InitError(f"unsafe or invalid site id {site_id!r}: must match {SITE_ID_PATTERN}")
    return site_id


def register_path(site_id: str, register_dir: Path = ir.REGISTER_DIR) -> Path:
    site_id = _check_site_id(site_id)
    root = Path(register_dir)
    path = root / f"{site_id}.yaml"
    if path.parent.resolve() != root.resolve() or path.name != f"{site_id}.yaml":
        raise InitError(f"register path for {site_id!r} escapes the register directory")
    return path


def _label(path: Path, register_dir: Path) -> str:
    return (f"config/input_resolution/{path.name}" if Path(register_dir).resolve() == ir.REGISTER_DIR.resolve()
            else f"<register_dir>/{path.name}")


def pending_inputs(cfg, model: str = "delft3d") -> list[dict]:
    """Inputs the production gate lists as blocking and not READY -- what `report` will call
    UNRESOLVED until a reviewer-backed entry exists. Reuses `production_gate.config_items`."""
    from backend.m3_dflowfm import production_gate as pg
    return [{"target": i["key"], "gate_status": i["status"]}
            for i in pg.config_items(cfg, model) if i["blocking"] and i["status"] != pg.READY]


def render(site_id: str, pending: list[dict]) -> str:
    header = [
        f"# Input-resolution register for site '{site_id}' -- initialised EMPTY by `python -m {GENERATOR}`",
        f"# (generator version {GENERATOR_VERSION}). Same format as every register (backend/shared/input_resolution.py).",
        "#",
        "# Nothing here is evidence and nothing is approved. Add an entry only with a proposed value, a",
        "# docs/data_sources.md source_id and a verbatim quote; `approval` is filled in by a named reviewer,",
        "# never by tooling. Inputs with no entry are UNRESOLVED in `python -m backend.shared.input_resolution",
        f"# report {site_id}` (the source of truth; the list below is the production gate's view at init time).",
        "#",
        f"# Pending at initialisation ({len(pending)}):",
    ] + [f"#   UNRESOLVED  {p['target']}  (gate: {p['gate_status']})" for p in pending] + ["#"]
    body = yaml.safe_dump({"site_id": site_id, "entries": [], "evidence_only": []}, sort_keys=False)
    return "\n".join(header) + "\n" + body


def init_register(site_id: str, *, data_dir: str | Path | None = None, sites_dir: str | Path | None = None,
                  register_dir: Path = ir.REGISTER_DIR, dry_run: bool = False, model: str = "delft3d") -> dict:
    """Create `<register_dir>/<site_id>.yaml` if (and only if) it does not exist. Returns
    `{site_id, status: created|exists|dry_run, register, pending, problems}`; raises InitError."""
    path = register_path(site_id, register_dir)
    from backend.m3_dflowfm.preflight import _load_config
    data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    try:
        cfg, used = _load_config(site_id, data_dir, Path(sites_dir) if sites_dir else None)
    except Exception as e:  # noqa: BLE001 -- an unknown/invalid site gets no register
        raise InitError(f"no loadable site config for {site_id!r} ({type(e).__name__})") from None
    if cfg.site.id != site_id:
        raise InitError(f"site config id {cfg.site.id!r} != {site_id!r}")
    result = {"site_id": site_id, "register": _label(path, register_dir), "generator_version": GENERATOR_VERSION}
    if path.exists() or path.is_symlink():
        return {**result, "status": EXISTS, "pending": None, "problems": [],
                "message": "register already exists: left untouched (no merge, no overwrite)"}
    pending = pending_inputs(cfg, model)
    text = render(site_id, pending)
    if dry_run:
        return {**result, "status": DRY_RUN, "pending": [p["target"] for p in pending], "problems": [], "text": text}
    Path(register_dir).mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:  # exclusive create: never replaces anything
            f.write(text)
    except FileExistsError:
        return {**result, "status": EXISTS, "pending": None, "problems": [],
                "message": "register appeared concurrently: left untouched"}
    problems = ir.validate(site_id, sites_dir=used or ir.SITES_DIR, register_dir=Path(register_dir))
    return {**result, "status": CREATED, "pending": [p["target"] for p in pending], "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise an empty input-resolution register (Feature 16 P3)")
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--sites-dir", type=Path)
    parser.add_argument("--register-dir", type=Path, default=ir.REGISTER_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print the register without writing it")
    args = parser.parse_args(argv)
    try:
        out = init_register(args.site_id, data_dir=args.data_dir, sites_dir=args.sites_dir,
                            register_dir=args.register_dir, dry_run=args.dry_run)
    except InitError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    text = out.pop("text", None)
    print(text if text is not None else json.dumps(out, indent=2, sort_keys=True))
    return 1 if out["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())

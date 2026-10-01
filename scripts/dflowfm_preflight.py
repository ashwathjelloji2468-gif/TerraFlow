#!/usr/bin/env python3
"""Read-only D-Flow FM readiness preflight. Never writes or modifies files.

Usage:
  python scripts/dflowfm_preflight.py <site_id> [--data-dir DIR] [--sites-dir DIR]
                                      [--kernel PATH] [--min-free-gb N] [--json]

Kernel: --kernel, else $SIH26_DFLOWFM_KERNEL, else the documented default
~/delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh. Data dir: --data-dir, else $SIH26_DATA_DIR,
else <repo>/data. Exit status: 0 = PASS, 1 = WARNING only, 2 = BLOCKED.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.m3_dflowfm.preflight import BLOCKED, WARNING, format_report, run_preflight  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site_id")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--sites-dir", type=Path)
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--min-free-gb", type=float)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = run_preflight(args.site_id, data_dir=args.data_dir, sites_dir=args.sites_dir,
                           kernel=args.kernel, min_free_gb=args.min_free_gb)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return {BLOCKED: 2, WARNING: 1}.get(report["overall"], 0)


if __name__ == "__main__":
    sys.exit(main())

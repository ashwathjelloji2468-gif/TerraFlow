"""Real DualSPHysics smoke harness for one generated near-field case (Feature 6).

Runs the configured GenCase + DualSPHysics binaries through the production launcher
(`launcher.run_case_sync`) and the production post-processor, then reports three separate
verdicts -- a solver that exits 0 is not automatically a run that kept its water:

- `solver_status`             -- launcher success (return codes, XML/particle/PART artifacts)
- `postprocess_status`        -- `postprocess.postprocess_run` wrote summary rasters + run_meta
- `particle_retention_status` -- `solver_log.particle_retention` on the solver's own log

Nothing is simulated or substituted here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import launcher, solver_log
from .settings import SphSettings


class BinariesUnavailable(RuntimeError):
    """`DSPH_BIN_DIR` (or `binaries_dir`) is missing a required real binary -- nothing is run."""


REQUIRED_BINARIES = ("GenCase_linux64", "DualSPHysics5.4_linux64", "MeasureTool_linux64", "IsoSurface_linux64")


def resolve_binaries(binaries_dir: str | Path | None = None) -> Path:
    raw = binaries_dir or os.environ.get("DSPH_BIN_DIR")
    if not raw:
        raise BinariesUnavailable("DSPH_BIN_DIR is not set")
    path = Path(raw).expanduser().resolve()
    missing = [n for n in REQUIRED_BINARIES if not (path / n).is_file() or not os.access(path / n, os.X_OK)]
    if missing:
        raise BinariesUnavailable(f"missing or non-executable in {path}: {', '.join(missing)}")
    return path


def run_smoke_case(case_dir: str | Path, run_dir: str | Path, terrain_dir: str | Path, settings: SphSettings,
                   binaries_dir: str | Path | None = None) -> dict:
    """Run an already-written case (`generator.write_case`) and write `<run_dir>/smoke_report.json`."""
    from .postprocess import postprocess_run

    bin_dir = resolve_binaries(binaries_dir)
    run_dir = Path(run_dir)
    execution = launcher.run_case_sync(case_dir, run_dir / "attempts" / "a00", bin_dir)
    report: dict = {"binaries_dir": str(bin_dir), "execution": execution,
                    "solver_status": "SUCCEEDED" if execution["success"] else "FAILED",
                    "postprocess_status": "BLOCKED", "particle_retention_status": "UNAVAILABLE"}
    log = run_dir / "raw" / "log.txt"
    facts = solver_log.parse_log(log.read_text(encoding="utf-8", errors="replace") if log.is_file() else None)
    retention = solver_log.particle_retention(facts, settings.max_excluded_particle_fraction)
    report["particle_retention"] = retention
    report["particle_retention_status"] = retention["status"]
    if execution["success"]:
        try:
            meta = postprocess_run(run_dir, terrain_dir, run_dir / "raw" / "data", settings=settings,
                                   binaries_dir=str(bin_dir), execution=execution)
            report["postprocess_status"] = "SUCCEEDED"
            report["run_meta"] = meta
        except Exception as e:  # reported, never hidden
            report["postprocess_status"] = "FAILED"
            report["postprocess_error"] = f"{type(e).__name__}: {e}"
    else:
        report["postprocess_reason"] = "solver did not succeed"
    (run_dir / "smoke_report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report

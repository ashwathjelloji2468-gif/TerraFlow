"""Detached DualSPHysics execution for campaign-managed M4 cases.

GenCase and DualSPHysics run as children of a short-lived launcher process. M0
tracks that launcher by PID and reads ``execution.json`` after it exits. Logs,
raw solver output, and one-second GPU samples are retained under the run folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.m0_api import runner

from . import solver_log


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binary_dir(value: str | Path | None) -> Path:
    raw = value or os.environ.get("DSPH_BIN_DIR")
    if not raw:
        raise RuntimeError("DualSPHysics binary directory is required (DSPH_BIN_DIR)")
    path = Path(raw).resolve()
    for name in ("GenCase_linux64", "DualSPHysics5.4_linux64"):
        if not (path / name).is_file():
            raise FileNotFoundError(f"missing DualSPHysics executable: {path / name}")
    return path


def _env(bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(bin_dir), env.get("LD_LIBRARY_PATH", "")]))
    return env


def _case_name(case_dir: Path) -> str:
    defs = sorted(case_dir.glob("*_Def.xml"))
    if len(defs) != 1:
        raise RuntimeError(f"expected exactly one *_Def.xml in {case_dir}, found {len(defs)}")
    return defs[0].name.removesuffix("_Def.xml")


def _run_logged(command: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> tuple[int, float]:
    started = time.monotonic()
    with log_path.open("wb") as log:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    return result.returncode, round(time.monotonic() - started, 3)


def _gpu_sample(bin_dir: Path, path: Path, stop: threading.Event, interval_s: float) -> None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "memory_used_mib"])
        while not stop.is_set():
            result = subprocess.run(
                [executable, "--query-gpu=timestamp,memory.used", "--format=csv,noheader,nounits"],
                env=_env(bin_dir), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False,
            )
            if result.returncode == 0:
                for row in csv.reader(result.stdout.splitlines()):
                    if len(row) >= 2:
                        writer.writerow([row[0].strip(), row[1].strip()])
                stream.flush()
            stop.wait(interval_s)


def _peak_vram_mib(path: Path) -> float | None:
    if not path.is_file():
        return None
    values = []
    for row in csv.DictReader(path.open(encoding="utf-8")):
        try:
            values.append(float(row["memory_used_mib"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(values) if values else None


def _gpu_identity(bin_dir: Path) -> dict:
    """GPU name / driver as `nvidia-smi` reports them; empty when it is unavailable."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"nvidia_smi": None}
    result = subprocess.run([executable, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                            env=_env(bin_dir), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, check=False)
    rows = [r for r in csv.reader(result.stdout.splitlines()) if len(r) >= 2] if result.returncode == 0 else []
    return {"nvidia_smi": executable, "gpu_name": rows[0][0].strip() if rows else None,
            "gpu_driver_version": rows[0][1].strip() if rows else None}


def _provenance(bin_dir: Path, gen_log: Path, solver_log_path: Path) -> dict:
    """Executable paths plus facts the real logs printed (kept for failed runs too)."""
    gen = solver_log.parse_log(gen_log.read_text(encoding="utf-8", errors="replace") if gen_log.is_file() else None)
    sol = solver_log.parse_log(solver_log_path.read_text(encoding="utf-8", errors="replace")
                               if solver_log_path.is_file() else None)
    return {"gencase_executable": str(bin_dir / "GenCase_linux64"),
            "solver_executable": str(bin_dir / "DualSPHysics5.4_linux64"),
            "gencase_version": gen["gencase_version"] or sol["gencase_version"],
            "gencase_particles": {k: gen[k] for k in ("total_particles", "bound_particles", "fluid_particles")},
            "solver_log_facts": sol, **_gpu_identity(bin_dir)}


def run_case_sync(case_dir: str | Path, run_dir: str | Path, binaries_dir: str | Path | None = None,
                  sample_interval_s: float = 1.0) -> dict:
    """Run GenCase then the GPU solver; write a truthful execution record.

    This function is used inside the detached launcher process. A nonzero exit,
    missing generated XML/initial particles, solver exception marker, or missing
    non-empty PART output is a failure. The solver exit code is retained.
    """
    case_dir, run_dir = Path(case_dir).resolve(), Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = _binary_dir(binaries_dir)
    case_name = _case_name(case_dir)
    env = _env(bin_dir)
    start_at = _utc_now()
    start_wall = time.monotonic()
    gen_log, solver_log_path = run_dir / "gencase.log", run_dir / "solver.log"
    root_run_dir = run_dir.parents[1] if run_dir.parent.name == "attempts" else run_dir.parent
    raw_dir = root_run_dir / "raw"
    if raw_dir.exists() and any(raw_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite existing raw solver output: {raw_dir}")
    raw_dir.mkdir(parents=True, exist_ok=True)

    gen_rc, gen_wall = _run_logged(
        [str(bin_dir / "GenCase_linux64"), f"{case_name}_Def", case_name], case_dir, env, gen_log,
    )
    gen_xml = case_dir / f"{case_name}.xml"
    gen_particles = case_dir / f"{case_name}.bi4"
    solver_rc: int | None = None
    solver_wall: float | None = None
    stop_sample = threading.Event()
    sampler: threading.Thread | None = None
    sample_path = run_dir / "nvidia_smi.csv"
    error: str | None = None

    if gen_rc != 0:
        error = f"GenCase exited with status {gen_rc}"
    elif not gen_xml.is_file() or gen_xml.stat().st_size == 0 or not gen_particles.is_file() or gen_particles.stat().st_size == 0:
        error = "GenCase did not write non-empty XML and initial particle artifacts"
    else:
        sampler = threading.Thread(target=_gpu_sample, args=(bin_dir, sample_path, stop_sample, sample_interval_s), daemon=True)
        sampler.start()
        solver_rc, solver_wall = _run_logged(
            [str(bin_dir / "DualSPHysics5.4_linux64"), "-gpu", "-name", case_name,
             "-dirout", str(raw_dir), "-dirdataout", "data"], case_dir, env, solver_log_path,
        )
        stop_sample.set()
        sampler.join(timeout=max(2.0, sample_interval_s * 2.0))
        solver_text = solver_log_path.read_text(encoding="utf-8", errors="replace")
        # Kept for failed runs too, so post-mortems can read the solver's own version/exclusions.
        shutil.copyfile(solver_log_path, raw_dir / "log.txt")
        part_files = [p for p in (raw_dir / "data").glob("Part_*.bi4") if p.is_file() and p.stat().st_size]
        if solver_rc != 0:
            error = f"DualSPHysics exited with status {solver_rc}"
        elif re.search(r"\*\*\* Exception|Finished execution \(code=(?!0\))", solver_text):
            error = "DualSPHysics log reports an exception or unsuccessful completion"
        elif not (raw_dir / "Run.out").is_file() or not part_files:
            error = "DualSPHysics did not write non-empty Run.out and PART artifacts"

    stop_sample.set()
    if sampler is not None:
        sampler.join(timeout=max(2.0, sample_interval_s * 2.0))
    finished_at = _utc_now()
    result = {
        "success": error is None,
        "solver": "DualSPHysics",
        "case_name": case_name,
        "case_dir": str(case_dir),
        "raw_dir": str(raw_dir),
        "started_at": start_at,
        "finished_at": finished_at,
        "wall_time_s": round(time.monotonic() - start_wall, 3),
        "gencase_returncode": gen_rc,
        "gencase_wall_time_s": gen_wall,
        "solver_returncode": solver_rc,
        "solver_wall_time_s": solver_wall,
        "peak_vram_mb": _peak_vram_mib(sample_path),
        "gpu_sample_interval_s": sample_interval_s,
        "nvidia_smi_log": str(sample_path) if sample_path.is_file() else None,
        "error": error,
        "provenance": _provenance(bin_dir, gen_log, solver_log_path),
    }
    (run_dir / "execution.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def launch_case(case_dir: str | Path, run_dir: str | Path, binaries_dir: str | Path | None = None,
                attempt: int = 0) -> subprocess.Popen:
    """Start a detached launcher under ``runs/<run_id>/attempts/aNN``."""
    attempt_dir = Path(run_dir) / "attempts" / f"a{attempt:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "backend.m4_sph.launcher", "--case-dir", str(Path(case_dir).resolve()),
           "--attempt-dir", str(attempt_dir.resolve())]
    if binaries_dir:
        cmd += ["--binaries-dir", str(binaries_dir)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")])))
    return runner.launch_detached(cmd, attempt_dir, env=env)


def read_result(run_dir: str | Path, attempt: int) -> dict | None:
    path = Path(run_dir) / "attempts" / f"a{attempt:02d}" / "execution.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def archive_failed_output(run_dir: str | Path, attempt: int) -> Path | None:
    """Move an unsuccessful attempt's raw output under its attempt folder intact."""
    root = Path(run_dir)
    raw = root / "raw"
    if not raw.exists():
        return None
    target = root / "attempts" / f"a{attempt:02d}" / "failed_raw"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    raw.rename(target)
    return target


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--binaries-dir", default=None)
    parser.add_argument("--sample-interval-s", type=float, default=1.0)
    args = parser.parse_args(argv)
    result = run_case_sync(args.case_dir, args.attempt_dir, args.binaries_dir, args.sample_interval_s)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())

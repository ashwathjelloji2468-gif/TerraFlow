"""Detached D-Flow FM launches and progress/success checks.

The kernel exit status is intentionally ignored. M3 rule 1 is the sole success
criterion: no ``** ERROR`` line in the diagnostic file and both result files.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.m0_api import runner

DEFAULT_KERNEL = Path(os.environ.get(
    "SIH26_DFLOWFM_KERNEL",
    str(Path.home() / "delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh"),
))
_KERNEL_OVERRIDE = os.environ.get("SIH26_DFLOWFM_KERNEL")
_TIME_RE = re.compile(
    r"(?:simulation\s+time|current\s+time|simulation\s+period\s*\(s\))\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
    re.I,
)
_TSTOP_RE = re.compile(r"^\s*tStop\s*=\s*([-+0-9.eE]+)", re.I | re.M)


@dataclass(frozen=True)
class DFlowProgress:
    steps_done: int
    steps_total: int | None
    finished: bool
    ok: bool


def _without_mnt_path(env: dict[str, str]) -> dict[str, str]:
    env = dict(env)
    env["PATH"] = os.pathsep.join(
        item for item in env.get("PATH", "").split(os.pathsep)
        if not item.startswith("/mnt/")
    )
    return env


def _wsl_path(path: Path) -> str:
    return subprocess.check_output(["wsl", "wslpath", "-u", str(path)], text=True).strip()


def launch_case(case_dir: str | Path, run_dir: str | Path, *, model: str = "model.mdu",
                kernel: str | Path = DEFAULT_KERNEL) -> subprocess.Popen:
    """Start the M3 case detached, writing combined stdout/stderr to M0's log."""
    case_dir, run_dir = Path(case_dir).resolve(), Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    output = case_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    stem = Path(model).stem
    for suffix in ("dia", "map.nc", "his.nc"):
        artifact = output / f"{stem}_{suffix}" if suffix != "dia" else output / f"{stem}.dia"
        artifact.unlink(missing_ok=True)
    (output / "resource_usage.txt").unlink(missing_ok=True)
    log = open(runner.log_path(run_dir), "ab")
    if os.name == "nt":
        wsl_case = _wsl_path(case_dir)
        wsl_kernel = ("~/delft3d/dflowfm-2026.01/lnx64/bin/run_dflowfm.sh"
                      if not _KERNEL_OVERRIDE and Path(kernel) == DEFAULT_KERNEL
                      else shlex.quote(_wsl_path(Path(kernel))))
        # Quote all paths for bash; using `bash -lc` also permits the required PATH cleanup.
        timer = ("/usr/bin/time -v -o output/resource_usage.txt "
                 if subprocess.run(["wsl", "bash", "-lc", "test -x /usr/bin/time"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 else "")
        script = (f"cd {shlex.quote(wsl_case)} && "
                  "PATH=$(echo \"$PATH\" | tr : '\\n' | grep -v '^/mnt/' | paste -sd:) && "
                  f"{timer}{wsl_kernel} {shlex.quote(model)}")
        return subprocess.Popen(["wsl", "bash", "-lc", script], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    env = _without_mnt_path(dict(os.environ))
    command = (["/usr/bin/time", "-v", "-o", str(output / "resource_usage.txt"), str(kernel), model]
               if Path("/usr/bin/time").is_file() else [str(kernel), model])
    return subprocess.Popen(command, cwd=case_dir, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True)


def check_success(case_dir: str | Path, model_stem: str) -> dict:
    """Evaluate only M3 rule 1; never inspect a process return code."""
    output = Path(case_dir) / "output"
    dia = output / f"{model_stem}.dia"
    errors = []
    if dia.is_file():
        errors = [line for line in dia.read_text(errors="replace").splitlines()
                  if line.startswith("** ERROR")]
    else:
        errors = [f"missing diagnostic file: {dia}"]
    missing = [str(output / f"{model_stem}_{suffix}.nc")
               for suffix in ("map", "his")
               if not (output / f"{model_stem}_{suffix}.nc").is_file()]
    return {"success": not errors and not missing, "dia_errors": errors,
            "missing_outputs": missing}


def read_progress(case_dir: str | Path, model_stem: str, total_s: float | None = None) -> DFlowProgress:
    """Parse the latest simulation time from the live `.dia`; terminal status uses rule 1."""
    case_dir = Path(case_dir)
    dia = case_dir / "output" / f"{model_stem}.dia"
    if total_s is None:
        mdu = case_dir / f"{model_stem}.mdu"
        if mdu.is_file() and (match := _TSTOP_RE.search(mdu.read_text(errors="replace"))):
            total_s = float(match.group(1))
    latest = 0.0
    if dia.is_file():
        for line in dia.read_text(errors="replace").splitlines():
            if match := _TIME_RE.search(line):
                latest = max(latest, float(match.group(1)))
    result = check_success(case_dir, model_stem)
    if result["success"]:
        return DFlowProgress(round(latest), round(total_s) if total_s else None, True, True)
    return DFlowProgress(round(latest), round(total_s) if total_s else None, False, False)


def is_alive(pid: int, case_dir: str | Path) -> bool:
    """Check the detached kernel by its case working directory, including WSL from Windows."""
    if os.name == "nt":
        try:
            wsl_case = shlex.quote(_wsl_path(Path(case_dir)))
            script = (
                f"for p in /proc/[0-9]*; do "
                f"[ \"$(readlink \"$p/cwd\" 2>/dev/null)\" = {wsl_case} ] || continue; "
                "tr '\\0' ' ' < \"$p/cmdline\" 2>/dev/null | grep -q dflowfm && exit 0; "
                "done; exit 1"
            )
            return subprocess.run(["wsl", "bash", "-lc", script],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        return Path(f"/proc/{pid}/cwd").resolve() == Path(case_dir).resolve()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


# --- Feature 17: completion evidence and failed-attempt archival -----------------------------------

_INTERVAL_RE = {
    "MapInterval": re.compile(r"^\s*MapInterval\s*=\s*([-+0-9.eE]+)", re.I | re.M),
    "HisInterval": re.compile(r"^\s*HisInterval\s*=\s*([-+0-9.eE]+)", re.I | re.M),
}
_TIME_UNIT_S = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}


def mdu_time_settings(case_dir: str | Path, model_stem: str) -> dict:
    """`TStop` / `MapInterval` / `HisInterval` exactly as written in the case's `.mdu` (the solver's own
    input). Missing values are None; nothing is defaulted."""
    mdu = Path(case_dir) / f"{model_stem}.mdu"
    if not mdu.is_file():
        return {"mdu": str(mdu), "present": False, "tstop_s": None, "map_interval_s": None, "his_interval_s": None}
    text = mdu.read_text(errors="replace")
    stop = _TSTOP_RE.search(text)
    out = {"mdu": str(mdu), "present": True, "tstop_s": float(stop.group(1)) if stop else None}
    for key, name in (("map_interval_s", "MapInterval"), ("his_interval_s", "HisInterval")):
        match = _INTERVAL_RE[name].search(text)
        out[key] = float(match.group(1)) if match else None
    return out


def final_output_time_s(nc_path: str | Path) -> float | None:
    """Last time record of a D-Flow FM `_map.nc` / `_his.nc` in model seconds (undecoded, using the
    file's own `units`; a file without units is read as seconds). None when unreadable or empty."""
    import xarray as xr

    try:
        with xr.open_dataset(nc_path, decode_times=False) as ds:
            if "time" not in ds.variables or ds["time"].size == 0:
                return None
            values = ds["time"].values.astype(float)
            units = str(ds["time"].attrs.get("units", "seconds")).strip().lower()
    except Exception:  # noqa: BLE001 - unreadable output is "no evidence", never a pass
        return None
    scale = next((s for word, s in _TIME_UNIT_S.items() if units.startswith(word)), None)
    if scale is None or values.size == 0:
        return None
    return float(values[-1]) * scale


def archive_failed_attempt(case_dir: str | Path, run_dir: str | Path, attempt: int, details: dict | None = None) -> Path:
    """Move a failed D-Flow FM attempt's solver output out of the way before a retry relaunches.

    `case/output/` (the `.dia`, `_map.nc`, `_his.nc`, resource usage) is MOVED intact to
    `runs/<run_id>/attempts/aNN/failed_output/`; the run log is COPIED there (the live log keeps
    appending); `attempt_meta.json` records why the attempt failed. An existing archive for the same
    attempt is never overwritten (FileExistsError)."""
    import json
    import shutil

    case_dir, run_dir = Path(case_dir), Path(run_dir)
    target = run_dir / "attempts" / f"a{int(attempt):02d}"
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(parents=True)
    output = case_dir / "output"
    moved = None
    if output.is_dir():
        output.rename(target / "failed_output")
        moved = str(target / "failed_output")
    log = runner.log_path(run_dir)
    if Path(log).is_file():
        shutil.copy2(log, target / Path(log).name)
    (target / "attempt_meta.json").write_text(json.dumps({
        "attempt": int(attempt), "case_dir": str(case_dir), "archived_output": moved,
        "details": details or {}}, indent=2, default=str) + "\n", encoding="utf-8")
    return target

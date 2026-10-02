"""Launch solver runs as detached processes and read their progress from logs.

CLAUDE.md rule 14: long solver runs never block the worker or the API. A run
starts in its own session (`start_new_session=True`) with stdout and stderr
going to `runs/<run_id>/log.txt`. It keeps running if the worker dies, and the
worker learns how far it has got only by reading that log.

Log format: the fake solver (`backend.m0_api.fake_solver`) writes
`PROGRESS k/N` lines and a final `DONE ok` or `DONE failed`. Real Delft3D /
DualSPHysics logs will need their own parser behind `read_progress` (M3, M4).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PROGRESS_RE = re.compile(r"^PROGRESS (\d+)/(\d+)\s*$")
_DONE_RE = re.compile(r"^DONE (ok|failed)\b")


@dataclass(frozen=True)
class RunProgress:
    steps_done: int
    steps_total: int | None
    finished: bool
    ok: bool


def log_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "log.txt"


def launch_detached(cmd: list[str], run_dir: str | Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start `cmd` in its own session, logging to `run_dir/log.txt`.

    Returns the Popen handle so the launching worker can reap the process when
    it exits. After a worker restart the process is no longer our child; init
    reaps it, and the new worker tracks it by PID and log only.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(log_path(run_dir), "ab") as log:
        return subprocess.Popen(
            cmd,
            cwd=run_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def read_progress(path: str | Path) -> RunProgress:
    """Latest `PROGRESS` line and whether a `DONE` line has been written."""
    done, total, finished, ok = 0, None, False, False
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        if m := _PROGRESS_RE.match(line):
            done, total = int(m.group(1)), int(m.group(2))
        elif m := _DONE_RE.match(line):
            finished, ok = True, m.group(1) == "ok"
    return RunProgress(done, total, finished, ok)


def tail(path: str | Path, n: int) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-n:]
    except FileNotFoundError:
        return []


_HAS_PROC = Path("/proc/self/stat").is_file()


def _proc_state_cmdline(pid: int) -> tuple[str, str] | None:
    """(state, command line) from /proc (Linux/WSL); None if the process is gone."""
    proc = Path(f"/proc/{pid}")
    try:
        state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    return state, cmdline


def _ps_state_cmdline(pid: int) -> tuple[str, str] | None:
    """(state, command line) from `ps` (macOS/BSD, no /proc); None if the process is gone."""
    try:
        # -ww: no column-width truncation (Linux procps and BSD/macOS ps), so the run id survives.
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    state, _, cmdline = line.partition(" ")
    return state, cmdline.strip()


def is_alive(pid: int, run_id: str) -> bool:
    """True if `pid` is a live (not zombie) process running `run_id`.

    Checking the command line guards against the OS having reused the PID for
    an unrelated process after the run exited. Reads /proc on Linux/WSL; falls
    back to `ps -ww -p <pid> -o stat= -o command=` where /proc is absent (macOS).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else; the cmdline check decides
    found = _proc_state_cmdline(pid) if _HAS_PROC else _ps_state_cmdline(pid)
    if found is None:
        return False
    state, cmdline = found
    # /proc: Z zombie, X dead. ps: state starts with Z for a zombie.
    return not state.startswith(("Z", "X")) and run_id in cmdline

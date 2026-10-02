"""`runner.is_alive` on both platforms' code paths, against real child processes.

Linux/WSL reads /proc; macOS has no /proc, so `is_alive` falls back to `ps`. The `ps` path is
exercised here on any POSIX host by forcing `_HAS_PROC = False`."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from backend.m0_api import runner

RUN_ID = "synth__s001__sph"


def _child(seconds: float) -> subprocess.Popen:
    # The run id appears in the command line, as it does for the real detached launcher.
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})", RUN_ID])


@pytest.fixture(params=["proc", "ps"])
def path(request, monkeypatch):
    if request.param == "proc" and not runner._HAS_PROC:
        pytest.skip("no /proc on this host")
    monkeypatch.setattr(runner, "_HAS_PROC", request.param == "proc")
    return request.param


def test_live_process_with_run_id_is_alive(path):
    proc = _child(30)
    try:
        assert runner.is_alive(proc.pid, RUN_ID) is True
        assert runner.is_alive(proc.pid, "some_other_run") is False  # PID-reuse guard
    finally:
        proc.kill()
        proc.wait()


def test_exited_process_is_not_alive(path):
    proc = _child(0)
    proc.wait()
    assert runner.is_alive(proc.pid, RUN_ID) is False


def test_zombie_is_not_alive(path):
    proc = _child(0)
    deadline = time.time() + 10
    while time.time() < deadline:  # exited but not yet reaped -> zombie
        state = (runner._proc_state_cmdline(proc.pid) if path == "proc" else runner._ps_state_cmdline(proc.pid))
        if state is None or state[0].startswith("Z"):
            break
        time.sleep(0.05)
    try:
        assert runner.is_alive(proc.pid, RUN_ID) is False
    finally:
        proc.wait()


def test_nonexistent_pid_is_not_alive(path):
    assert runner.is_alive(2 ** 22 + 12345, RUN_ID) is False
